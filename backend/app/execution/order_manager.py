"""Order Manager: the only component that talks to the exchange gateway for orders.

Guarantees
- Idempotency: client_order_id (= Upbit `identifier`) is unique in DB; a re-submit of the same
  id returns the stored record and never hits the exchange twice.
- Single in-flight order per market: a new order is refused while any order is SUBMITTED /
  OPEN / PARTIALLY_FILLED / UNKNOWN.
- UNKNOWN handling: a network error after the request may have reached the exchange leaves
  the order UNKNOWN; `recover_unknown()` queries by identifier and resolves it. While an
  UNKNOWN order exists, new orders are blocked (RiskManager sees unknown_orders > 0).
- Every state change is persisted before/after the exchange call and published as an event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.events import Event, EventBus, EventLevel, EventType
from app.core.logging import get_logger
from app.core.types import OrderIntent, OrderRecord, OrderState, OrdType, Side, TradingMode
from app.database.repository import Repository
from app.exchange.base import (
    DuplicateIdentifierError,
    ExchangeGateway,
    GatewayNetworkError,
    GatewayOrder,
    GatewayRejectedError,
    OrderNotFoundError,
)

log = get_logger("order_manager")


class OrderBlockedError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OrderManager:
    def __init__(
        self,
        gateway: ExchangeGateway,
        repo: Repository | None,
        bus: EventBus,
        mode: TradingMode,
        market: str,
        *,
        poll_timeout_sec: float = 30.0,
        poll_interval_sec: float = 0.5,
    ) -> None:
        self._gw = gateway
        self._repo = repo
        self._bus = bus
        self.mode = mode
        self.market = market
        self._poll_timeout = poll_timeout_sec
        self._poll_interval = poll_interval_sec
        self._orders: dict[str, OrderRecord] = {}  # in-memory mirror of non-terminal + recent
        self.consecutive_api_errors = 0
        self.submitted = 0
        self.failed = 0
        self.filled = 0
        self.duplicates_blocked = 0

    # ------------------------------------------------------------ queries
    def inflight(self) -> list[OrderRecord]:
        return [o for o in self._orders.values() if o.is_inflight]

    def unknown(self) -> list[OrderRecord]:
        return [o for o in self._orders.values() if o.state is OrderState.UNKNOWN]

    async def load(self) -> None:
        """Rehydrate in-flight/unknown orders from DB after a restart."""
        if self._repo is None:
            return
        for o in await self._repo.inflight_orders(self.mode):
            self._orders[o.client_order_id] = o
        if self._orders:
            log.warning("orders_rehydrated_inflight", count=len(self._orders))

    # ------------------------------------------------------------- submit
    async def submit(self, intent: OrderIntent) -> OrderRecord:
        if intent.mode is not self.mode:
            raise OrderBlockedError(f"MODE_MISMATCH {intent.mode} != {self.mode}")
        existing = self._orders.get(intent.client_order_id)
        if existing is None and self._repo is not None:
            existing = await self._repo.get_order(intent.client_order_id)
        if existing is not None:
            self.duplicates_blocked += 1
            log.warning("duplicate_client_order_id", client_order_id=intent.client_order_id)
            return existing
        if self.unknown():
            raise OrderBlockedError("UNKNOWN_ORDER_PENDING")
        if self.inflight():
            self.duplicates_blocked += 1
            raise OrderBlockedError("INFLIGHT_ORDER")

        now = datetime.now(UTC)
        rec = OrderRecord(
            client_order_id=intent.client_order_id,
            mode=self.mode,
            market=intent.market,
            side=intent.side,
            ord_type=intent.ord_type,
            state=OrderState.CREATED,
            created_at=now,
            updated_at=now,
            req_krw=intent.krw_amount,
            req_volume=intent.volume,
            signal_id=intent.signal_id,
        )
        self._orders[rec.client_order_id] = rec
        await self._persist(rec)
        await self._emit(EventType.ORDER_CREATED, rec)

        rec.state = OrderState.SUBMITTED
        rec.submitted_at = datetime.now(UTC)
        await self._persist(rec)  # persist BEFORE the network call so a crash leaves a trace
        try:
            if intent.side is Side.BID:
                assert intent.krw_amount is not None and intent.ord_type is OrdType.PRICE
                gw = await self._gw.place_market_buy(rec.client_order_id, intent.krw_amount)
            else:
                assert intent.volume is not None and intent.ord_type is OrdType.MARKET
                gw = await self._gw.place_market_sell(rec.client_order_id, intent.volume)
            self.consecutive_api_errors = 0
            self.submitted += 1
            await self._emit(EventType.ORDER_SUBMITTED, rec)
        except GatewayRejectedError as exc:
            rec.state = OrderState.REJECTED
            rec.error_code = exc.code
            self.failed += 1
            await self._persist(rec)
            await self._emit(EventType.ORDER_REJECTED, rec, EventLevel.WARN, error=exc.code)
            return rec
        except DuplicateIdentifierError:
            # Exchange already has it (e.g. retry after crash) -> resolve by lookup.
            rec.state = OrderState.UNKNOWN
            rec.error_code = "duplicate_identifier"
            await self._persist(rec)
            return await self._resolve_unknown(rec)
        except GatewayNetworkError as exc:
            self.consecutive_api_errors += 1
            rec.state = OrderState.UNKNOWN
            rec.error_code = f"network: {exc}"
            await self._persist(rec)
            await self._emit(EventType.ORDER_UNKNOWN, rec, EventLevel.ERROR, error=str(exc))
            return await self._resolve_unknown(rec)

        self._apply_gateway(rec, gw)
        if not rec.is_terminal:
            await self._poll_until_terminal(rec)
        await self._persist(rec)
        await self._emit_terminal(rec)
        return rec

    # ------------------------------------------------------------ recovery
    async def recover_unknown(self) -> list[OrderRecord]:
        out = []
        for rec in list(self.unknown()):
            out.append(await self._resolve_unknown(rec))
        return out

    async def resolve_inflight(self) -> list[OrderRecord]:
        """After a restart: ask the exchange about every SUBMITTED/OPEN/PARTIALLY_FILLED order we
        rehydrated (UNKNOWN ones go through recover_unknown). Market orders are expected to be
        terminal already; anything still open is polled and finally cancelled."""
        out: list[OrderRecord] = []
        for rec in list(self.inflight()):
            if rec.state is OrderState.UNKNOWN:
                continue
            try:
                gw = await self._gw.get_order(client_order_id=rec.client_order_id)
            except OrderNotFoundError:
                rec.state = OrderState.REJECTED
                rec.error_code = (rec.error_code or "") + " | not_found_after_restart"
                self.failed += 1
                await self._persist(rec)
                await self._emit(
                    EventType.ORDER_REJECTED, rec, EventLevel.WARN, error="not_found_after_restart"
                )
                out.append(rec)
                continue
            except GatewayNetworkError as exc:
                self.consecutive_api_errors += 1
                log.error(
                    "inflight_order_lookup_failed",
                    client_order_id=rec.client_order_id,
                    error=str(exc),
                )
                continue
            self.consecutive_api_errors = 0
            self._apply_gateway(rec, gw)
            if not rec.is_terminal:
                await self._poll_until_terminal(rec)
            await self._persist(rec)
            await self._emit_terminal(rec)
            out.append(rec)
        return out

    async def _resolve_unknown(self, rec: OrderRecord) -> OrderRecord:
        try:
            gw = await self._gw.get_order(client_order_id=rec.client_order_id)
        except OrderNotFoundError:
            # Never reached the exchange -> safe to mark rejected (no position change).
            rec.state = OrderState.REJECTED
            rec.error_code = (rec.error_code or "") + " | not_found_after_unknown"
            self.failed += 1
            await self._persist(rec)
            await self._emit(
                EventType.ORDER_REJECTED, rec, EventLevel.WARN, error="not_found_after_unknown"
            )
            return rec
        except GatewayNetworkError as exc:
            self.consecutive_api_errors += 1
            log.error(
                "unknown_order_lookup_failed", client_order_id=rec.client_order_id, error=str(exc)
            )
            await self._persist(rec)
            return rec  # stays UNKNOWN; blocks new orders until resolved
        self.consecutive_api_errors = 0
        self._apply_gateway(rec, gw)
        if not rec.is_terminal:
            await self._poll_until_terminal(rec)
        await self._persist(rec)
        await self._emit_terminal(rec)
        return rec

    async def _poll_until_terminal(self, rec: OrderRecord) -> None:
        deadline = asyncio.get_running_loop().time() + self._poll_timeout
        delay = self._poll_interval
        while not rec.is_terminal and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
            try:
                gw = await self._gw.get_order(client_order_id=rec.client_order_id)
                self._apply_gateway(rec, gw)
            except GatewayNetworkError as exc:
                self.consecutive_api_errors += 1
                log.warning("order_poll_failed", error=str(exc))
        if not rec.is_terminal:
            # Market orders should never linger. Cancel remaining and settle what filled.
            try:
                gw = await self._gw.cancel_order(client_order_id=rec.client_order_id)
                self._apply_gateway(rec, gw)
            except (GatewayNetworkError, OrderNotFoundError) as exc:
                log.error("order_cancel_after_timeout_failed", error=str(exc))
                rec.state = OrderState.UNKNOWN
                rec.error_code = "timeout_then_cancel_failed"

    # ------------------------------------------------------------ helpers
    def _apply_gateway(self, rec: OrderRecord, gw: GatewayOrder) -> None:
        rec.exchange_uuid = gw.exchange_uuid or rec.exchange_uuid
        rec.executed_volume = gw.executed_volume
        rec.avg_fill_price = gw.avg_price
        rec.paid_fee = gw.paid_fee
        rec.fills = list(gw.fills)
        rec.raw = {**rec.raw, **gw.raw}
        rec.error_code = gw.error_code or rec.error_code
        if gw.state is OrderState.CANCELLED and gw.executed_volume > 0:
            rec.state = OrderState.CANCELLED  # partial then cancelled (Upbit market-sell remainder)
            rec.raw["partial"] = True
        elif gw.state in (OrderState.SUBMITTED, OrderState.OPEN) and gw.executed_volume > 0:
            rec.state = OrderState.PARTIALLY_FILLED
        else:
            rec.state = gw.state
        rec.updated_at = datetime.now(UTC)
        if rec.state is OrderState.FILLED:
            self.filled += 1

    async def _persist(self, rec: OrderRecord) -> None:
        rec.updated_at = datetime.now(UTC)
        if self._repo is not None:
            await self._repo.save_order(rec)

    async def _emit(
        self, type_: EventType, rec: OrderRecord, level: EventLevel = EventLevel.INFO, **extra: Any
    ) -> None:
        await self._bus.publish(
            Event(
                type_,
                f"{type_.value} {rec.side.value} {rec.client_order_id}",
                level,
                {
                    "client_order_id": rec.client_order_id,
                    "state": rec.state.value,
                    "side": rec.side.value,
                    "req_krw": str(rec.req_krw) if rec.req_krw is not None else None,
                    "req_volume": str(rec.req_volume) if rec.req_volume is not None else None,
                    "executed_volume": str(rec.executed_volume),
                    "avg_fill_price": str(rec.avg_fill_price) if rec.avg_fill_price else None,
                    **extra,
                },
            )
        )

    async def _emit_terminal(self, rec: OrderRecord) -> None:
        if rec.state is OrderState.FILLED:
            await self._emit(EventType.ORDER_FILLED, rec)
        elif rec.state is OrderState.CANCELLED:
            t = (
                EventType.ORDER_PARTIALLY_FILLED
                if rec.executed_volume > 0
                else EventType.ORDER_CANCELLED
            )
            await self._emit(t, rec, EventLevel.WARN)
        elif rec.state is OrderState.REJECTED:
            await self._emit(EventType.ORDER_REJECTED, rec, EventLevel.WARN)
        elif rec.state is OrderState.UNKNOWN:
            await self._emit(EventType.ORDER_UNKNOWN, rec, EventLevel.ERROR)


def new_client_order_id(run_id: str, seq: int, side: Side) -> str:
    """<= 64 chars, unique per account: run id + monotonic seq + side."""
    return f"{run_id}-{seq:06d}-{side.value}"[:64]


__all__ = ["OrderBlockedError", "OrderManager", "new_client_order_id"]
