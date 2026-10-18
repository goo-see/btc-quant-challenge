"""PAPER gateway: simulated exchange ledger with Upbit-like semantics.

- fills at the latest reference price through FillModel (fee + slippage)
- rejects like Upbit: insufficient funds, below min total, duplicate identifier
- fault injection for tests: network error after accept (-> UNKNOWN path), reject, partial fill
- ledger is serialisable so a restarted PAPER process resumes the same virtual account
Contains no code path that can reach the real order endpoint.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from app.core.types import Fill, OrderState, OrdType, Side
from app.exchange.base import (
    Balances,
    DuplicateIdentifierError,
    GatewayNetworkError,
    GatewayOrder,
    GatewayRejectedError,
    OrderNotFoundError,
)
from app.execution.fill_model import FillModel, FillParams, ZeroVolumeError
from app.portfolio.accounting import q_btc, q_krw

FaultKind = Literal["network_after_accept", "network_before_accept", "reject", "partial"]


@dataclass(slots=True)
class PaperLedger:
    krw: Decimal
    btc: Decimal = Decimal(0)
    btc_cost: Decimal = Decimal(0)  # for avg buy price
    fees_paid: Decimal = Decimal(0)

    def to_json(self) -> dict[str, str]:
        return {
            k: str(v)
            for k, v in {
                "krw": self.krw,
                "btc": self.btc,
                "btc_cost": self.btc_cost,
                "fees_paid": self.fees_paid,
            }.items()
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PaperLedger:
        return cls(
            Decimal(d["krw"]),
            Decimal(d["btc"]),
            Decimal(d.get("btc_cost", "0")),
            Decimal(d.get("fees_paid", "0")),
        )


@dataclass(slots=True)
class PaperFaults:
    next_place: FaultKind | None = None
    partial_ratio: Decimal = Decimal("0.5")
    get_order_failures: int = 0  # consecutive get_order network failures to simulate
    ping_ok: bool = True
    price_override: Decimal | None = None


class PaperGateway:
    name = "paper"

    def __init__(
        self,
        *,
        market: str,
        initial_krw: Decimal,
        fill_params: FillParams,
        min_order_krw: Decimal = Decimal(5000),
        ledger: PaperLedger | None = None,
    ) -> None:
        self.market = market
        self.ledger = ledger or PaperLedger(krw=initial_krw)
        self.fill_model = FillModel(fill_params)
        self.min_order_krw = min_order_krw
        self._price: Decimal | None = None
        self._price_ts: datetime | None = None
        self._orders: dict[str, GatewayOrder] = {}
        self.faults = PaperFaults()
        self.place_calls = 0

    # ---------------------------------------------------------------- feed
    def update_price(self, price: Decimal, ts: datetime | None = None) -> None:
        self._price = price
        self._price_ts = ts or datetime.now(UTC)

    def _ref_price(self) -> Decimal:
        if self.faults.price_override is not None:
            return self.faults.price_override
        if self._price is None:
            raise GatewayRejectedError("no_reference_price", "paper gateway has no price yet")
        return self._price

    # -------------------------------------------------------------- orders
    def _new_order(
        self, cid: str, side: Side, ord_type: OrdType, krw: Decimal | None, vol: Decimal | None
    ) -> GatewayOrder:
        return GatewayOrder(
            client_order_id=cid,
            exchange_uuid=str(uuid.uuid4()),
            market=self.market,
            side=side,
            ord_type=ord_type,
            state=OrderState.SUBMITTED,
            requested_krw=krw,
            requested_volume=vol,
            created_at=datetime.now(UTC),
        )

    def _pre_place(self, cid: str) -> None:
        self.place_calls += 1
        if cid in self._orders:
            raise DuplicateIdentifierError(cid)
        if self.faults.next_place == "network_before_accept":
            self.faults.next_place = None
            raise GatewayNetworkError("connect timeout before the exchange saw the order")
        if self.faults.next_place == "reject":
            self.faults.next_place = None
            raise GatewayRejectedError("market_offline", "injected reject")

    def _post_place(self, order: GatewayOrder) -> GatewayOrder:
        if self.faults.next_place == "network_after_accept":
            self.faults.next_place = None
            raise GatewayNetworkError("read timeout after the exchange accepted the order")
        return order

    async def place_market_buy(self, client_order_id: str, krw_amount: Decimal) -> GatewayOrder:
        self._pre_place(client_order_id)
        if krw_amount < self.min_order_krw:
            raise GatewayRejectedError(
                "under_min_total_bid", f"{krw_amount} < {self.min_order_krw}"
            )
        fee_est = q_krw(krw_amount * self.fill_model.p.fee_rate)
        if krw_amount + fee_est > self.ledger.krw:
            raise GatewayRejectedError(
                "insufficient_funds_bid", f"need {krw_amount + fee_est}, have {self.ledger.krw}"
            )
        order = self._new_order(client_order_id, Side.BID, OrdType.PRICE, krw_amount, None)
        self._orders[client_order_id] = order
        try:
            raw_fill = self.fill_model.market_buy(
                client_order_id, krw_amount, self._ref_price(), datetime.now(UTC)
            )
        except ZeroVolumeError as exc:
            del self._orders[client_order_id]
            raise GatewayRejectedError("under_min_volume", str(exc)) from exc
        fill = replace(raw_fill, exchange_trade_uuid=str(uuid.uuid4()))
        self._apply_fill(order, fill)
        order.state = OrderState.FILLED
        return self._post_place(order)

    async def place_market_sell(self, client_order_id: str, volume: Decimal) -> GatewayOrder:
        self._pre_place(client_order_id)
        price = self._ref_price()
        if volume * price < self.min_order_krw:
            raise GatewayRejectedError(
                "under_min_total_ask", f"{volume * price} < {self.min_order_krw}"
            )
        if volume > self.ledger.btc:
            raise GatewayRejectedError(
                "insufficient_funds_ask", f"sell {volume}, have {self.ledger.btc}"
            )
        order = self._new_order(client_order_id, Side.ASK, OrdType.MARKET, None, volume)
        self._orders[client_order_id] = order
        fill_vol = volume
        if self.faults.next_place == "partial":
            self.faults.next_place = None
            fill_vol = q_btc(volume * self.faults.partial_ratio)
        raw_fill = self.fill_model.market_sell(client_order_id, fill_vol, price, datetime.now(UTC))
        fill = replace(raw_fill, exchange_trade_uuid=str(uuid.uuid4()))
        self._apply_fill(order, fill)
        # Upbit: a market sell that cannot fully execute ends as `cancel` with executed_volume > 0
        order.state = OrderState.FILLED if fill_vol == volume else OrderState.CANCELLED
        return self._post_place(order)

    def _apply_fill(self, order: GatewayOrder, fill: Fill) -> None:
        if order.side is Side.BID:
            self.ledger.krw = q_krw(self.ledger.krw - fill.funds - fill.fee)
            self.ledger.btc = q_btc(self.ledger.btc + fill.volume)
            self.ledger.btc_cost += fill.funds
        else:
            portion = fill.volume / self.ledger.btc if self.ledger.btc > 0 else Decimal(1)
            self.ledger.btc_cost -= self.ledger.btc_cost * portion
            self.ledger.btc = q_btc(self.ledger.btc - fill.volume)
            self.ledger.krw = q_krw(self.ledger.krw + fill.funds - fill.fee)
            if self.ledger.btc == 0:
                self.ledger.btc_cost = Decimal(0)
        self.ledger.fees_paid += fill.fee
        order.fills.append(fill)
        order.executed_volume += fill.volume
        order.paid_fee += fill.fee
        total_funds = sum((f.funds for f in order.fills), Decimal(0))
        order.avg_price = total_funds / order.executed_volume if order.executed_volume else None

    async def get_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        if self.faults.get_order_failures > 0:
            self.faults.get_order_failures -= 1
            raise GatewayNetworkError("injected get_order failure")
        if client_order_id and client_order_id in self._orders:
            return self._orders[client_order_id]
        if exchange_uuid:
            for o in self._orders.values():
                if o.exchange_uuid == exchange_uuid:
                    return o
        raise OrderNotFoundError(client_order_id or exchange_uuid or "?")

    async def cancel_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        o = await self.get_order(client_order_id=client_order_id, exchange_uuid=exchange_uuid)
        if o.is_terminal:
            return o
        o.state = OrderState.CANCELLED
        return o

    async def get_open_orders(self) -> list[GatewayOrder]:
        return [o for o in self._orders.values() if not o.is_terminal]

    async def get_balances(self) -> Balances:
        avg = self.ledger.btc_cost / self.ledger.btc if self.ledger.btc > 0 else None
        return Balances(
            krw=self.ledger.krw,
            krw_locked=Decimal(0),
            btc=self.ledger.btc,
            btc_locked=Decimal(0),
            btc_avg_buy_price=avg,
        )

    async def get_reference_price(self) -> Decimal:
        return self._ref_price()

    async def ping(self) -> bool:
        return self.faults.ping_ok

    # -------------------------------------------------------- persistence
    def to_json(self) -> dict[str, Any]:
        return {"ledger": self.ledger.to_json()}

    def restore(self, d: dict[str, Any]) -> None:
        self.ledger = PaperLedger.from_json(d["ledger"])
