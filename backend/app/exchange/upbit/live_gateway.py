"""LIVE gateway: the ONLY code path that can place real Upbit orders.

Constructed exclusively by app.main_trader when TRADING_MODE=LIVE has passed live_guard; the
PAPER path never imports this module. Identifier (= client_order_id) makes creation idempotent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.core.types import Fill, OrderState, OrdType, Side
from app.exchange.base import (
    Balances,
    DuplicateIdentifierError,
    GatewayNetworkError,
    GatewayOrder,
    GatewayRejectedError,
    OrderNotFoundError,
)
from app.exchange.upbit.errors import UpbitHTTPError, UpbitNetworkError, UpbitRateLimitedError
from app.exchange.upbit.models import UpbitOrder
from app.exchange.upbit.rest_client import UpbitRestClient

log = get_logger("upbit.live")

_STATE = {
    "wait": OrderState.OPEN,
    "watch": OrderState.OPEN,
    "done": OrderState.FILLED,
    "cancel": OrderState.CANCELLED,
}


def _ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def to_gateway_order(o: UpbitOrder, client_order_id: str | None = None) -> GatewayOrder:
    side = Side(o.side)
    fills = [
        Fill(
            order_client_id=o.identifier or client_order_id or o.uuid,
            price=t.price,
            volume=t.volume,
            funds=t.funds,
            fee=Decimal(0),  # Upbit reports fees per order (paid_fee), not per trade
            timestamp=_ts(t.created_at),
            exchange_trade_uuid=t.uuid,
        )
        for t in o.trades
    ]
    if fills and o.paid_fee:
        # allocate the order-level fee across fills proportionally to funds
        total = sum((f.funds for f in fills), Decimal(0)) or Decimal(1)
        fills = [
            replace(f, fee=(o.paid_fee * f.funds / total).quantize(Decimal("0.01"))) for f in fills
        ]
    executed = o.executed_volume
    avg = (sum((f.funds for f in fills), Decimal(0)) / executed) if executed and fills else None
    return GatewayOrder(
        client_order_id=o.identifier or client_order_id or o.uuid,
        exchange_uuid=o.uuid,
        market=o.market,
        side=side,
        ord_type=OrdType(o.ord_type),
        state=_STATE.get(o.state, OrderState.UNKNOWN),
        requested_krw=o.price if o.ord_type == "price" else None,
        requested_volume=o.volume if o.ord_type != "price" else None,
        executed_volume=executed,
        avg_price=avg,
        paid_fee=o.paid_fee,
        fills=fills,
        created_at=_ts(o.created_at),
        raw={"state": o.state, "uuid": o.uuid, "trades_count": o.trades_count},
    )


class LiveGateway:
    name = "upbit-live"

    def __init__(self, client: UpbitRestClient, market: str) -> None:
        self._c = client
        self.market = market

    # ---------------------------------------------------------------- orders
    async def _place(
        self, cid: str, *, side: str, ord_type: str, price: Decimal | None, volume: Decimal | None
    ) -> GatewayOrder:
        try:
            o = await self._c.create_order(
                market=self.market,
                side=side,
                ord_type=ord_type,
                price=price,
                volume=volume,
                identifier=cid,
            )
        except UpbitNetworkError as exc:
            raise GatewayNetworkError(str(exc)) from exc
        except UpbitRateLimitedError as exc:
            raise GatewayNetworkError(f"rate_limited {exc.status}") from exc
        except UpbitHTTPError as exc:
            name = exc.name or f"http_{exc.status}"
            if "identifier" in name or "duplicate" in name:
                raise DuplicateIdentifierError(cid) from exc
            raise GatewayRejectedError(name, exc.message or "") from exc
        log.warning(
            "live_order_placed", client_order_id=cid, uuid=o.uuid, side=side, ord_type=ord_type
        )
        return to_gateway_order(o, cid)

    async def place_market_buy(self, client_order_id: str, krw_amount: Decimal) -> GatewayOrder:
        return await self._place(
            client_order_id,
            side="bid",
            ord_type="price",
            price=krw_amount.quantize(Decimal(1)),
            volume=None,
        )

    async def place_market_sell(self, client_order_id: str, volume: Decimal) -> GatewayOrder:
        return await self._place(
            client_order_id, side="ask", ord_type="market", price=None, volume=volume
        )

    async def get_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        try:
            o = await self._c.get_order(uuid=exchange_uuid, identifier=client_order_id)
        except UpbitNetworkError as exc:
            raise GatewayNetworkError(str(exc)) from exc
        except UpbitHTTPError as exc:
            if exc.status == 404 or (exc.name or "").endswith("not_found"):
                raise OrderNotFoundError(client_order_id or exchange_uuid or "?") from exc
            raise GatewayNetworkError(f"{exc.status} {exc.name}") from exc
        return to_gateway_order(o, client_order_id)

    async def cancel_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        try:
            o = await self._c.cancel_order(uuid=exchange_uuid, identifier=client_order_id)
        except UpbitNetworkError as exc:
            raise GatewayNetworkError(str(exc)) from exc
        except UpbitHTTPError as exc:
            if exc.status == 404:
                raise OrderNotFoundError(client_order_id or exchange_uuid or "?") from exc
            raise GatewayNetworkError(f"{exc.status} {exc.name}") from exc
        return to_gateway_order(o, client_order_id)

    async def get_open_orders(self) -> list[GatewayOrder]:
        try:
            rows = await self._c.get_open_orders(self.market, states=("wait", "watch"))
        except (UpbitNetworkError, UpbitHTTPError) as exc:
            raise GatewayNetworkError(str(exc)) from exc
        return [to_gateway_order(o) for o in rows]

    # -------------------------------------------------------------- balances
    async def get_balances(self) -> Balances:
        try:
            accts = await self._c.get_accounts()
        except (UpbitNetworkError, UpbitHTTPError) as exc:
            raise GatewayNetworkError(str(exc)) from exc
        krw = next((a for a in accts if a.currency == "KRW"), None)
        base = self.market.split("-")[1]
        coin = next((a for a in accts if a.currency == base), None)
        return Balances(
            krw=krw.balance if krw else Decimal(0),
            krw_locked=krw.locked if krw else Decimal(0),
            btc=coin.balance if coin else Decimal(0),
            btc_locked=coin.locked if coin else Decimal(0),
            btc_avg_buy_price=coin.avg_buy_price if coin and coin.balance > 0 else None,
            others={
                a.currency: a.balance + a.locked
                for a in accts
                if a.currency not in ("KRW", base) and (a.balance + a.locked) > 0
            },
        )

    async def get_reference_price(self) -> Decimal:
        return (await self._c.get_ticker(self.market)).trade_price

    async def ping(self) -> bool:
        try:
            await self._c.get_ticker(self.market)
            return True
        except (UpbitNetworkError, UpbitHTTPError):
            return False
