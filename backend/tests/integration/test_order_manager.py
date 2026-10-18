"""Order lifecycle, idempotency, UNKNOWN recovery, partial fills — against PaperGateway + SQLite."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.events import EventBus, EventType
from app.core.types import OrderIntent, OrderState, OrdType, Side, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.execution.order_manager import OrderBlockedError, OrderManager, new_client_order_id
from app.execution.paper_gateway import PaperGateway

P = FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(3))


class Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.types: list[EventType] = []
        bus.subscribe(self)

    async def __call__(self, ev) -> None:  # type: ignore[no-untyped-def]
        self.types.append(ev.type)


async def _setup(tmp_path: Path) -> tuple[OrderManager, PaperGateway, Repository, Recorder]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/om.db")
    await init_schema(engine)
    repo = Repository(engine)
    gw = PaperGateway(market="KRW-BTC", initial_krw=Decimal(100000), fill_params=P)
    gw.update_price(Decimal(100_000_000))
    bus = EventBus()
    rec = Recorder(bus)
    om = OrderManager(
        gw, repo, bus, TradingMode.PAPER, "KRW-BTC", poll_timeout_sec=1, poll_interval_sec=0.01
    )
    return om, gw, repo, rec


def _buy(cid: str, krw: int = 30000) -> OrderIntent:
    return OrderIntent(
        cid, "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(krw), None, None, TradingMode.PAPER
    )


def _sell(cid: str, vol: Decimal) -> OrderIntent:
    return OrderIntent(cid, "KRW-BTC", Side.ASK, OrdType.MARKET, None, vol, None, TradingMode.PAPER)


async def test_happy_path_created_submitted_filled_persisted(tmp_path: Path) -> None:
    om, gw, repo, rec = await _setup(tmp_path)
    r = await om.submit(_buy("run-000001-bid"))
    assert r.state is OrderState.FILLED and r.executed_volume > 0 and r.exchange_uuid
    assert rec.types == [EventType.ORDER_CREATED, EventType.ORDER_SUBMITTED, EventType.ORDER_FILLED]
    db = await repo.get_order("run-000001-bid")
    assert (
        db is not None
        and db.state is OrderState.FILLED
        and len(db.fills) == 1
        and db.db_id == r.db_id
    )
    assert om.inflight() == [] and om.filled == 1 and gw.place_calls == 1


async def test_idempotent_resubmit_never_hits_exchange_twice(tmp_path: Path) -> None:
    om, gw, repo, _ = await _setup(tmp_path)
    first = await om.submit(_buy("same-id"))
    again = await om.submit(_buy("same-id"))
    assert again.client_order_id == first.client_order_id and again.state is OrderState.FILLED
    assert gw.place_calls == 1 and om.duplicates_blocked == 1
    # a fresh OrderManager (process restart) with the same DB is still idempotent
    om2 = OrderManager(gw, repo, EventBus(), TradingMode.PAPER, "KRW-BTC")
    third = await om2.submit(_buy("same-id"))
    assert third.state is OrderState.FILLED and gw.place_calls == 1


async def test_rejected_order_is_terminal_and_recorded(tmp_path: Path) -> None:
    om, _, repo, rec = await _setup(tmp_path)
    r = await om.submit(_buy("rej", 4000))
    assert r.state is OrderState.REJECTED and r.error_code == "under_min_total_bid"
    assert EventType.ORDER_REJECTED in rec.types and om.failed == 1
    assert (await repo.get_order("rej")).state is OrderState.REJECTED  # type: ignore[union-attr]
    # rejected orders don't block the next one
    ok = await om.submit(_buy("next"))
    assert ok.state is OrderState.FILLED


async def test_unknown_after_accept_is_resolved_to_filled(tmp_path: Path) -> None:
    om, gw, _, rec = await _setup(tmp_path)
    gw.faults.next_place = "network_after_accept"
    r = await om.submit(_buy("u1"))
    assert r.state is OrderState.FILLED  # exchange had it; lookup by identifier resolved it
    assert EventType.ORDER_UNKNOWN in rec.types and rec.types[-1] is EventType.ORDER_FILLED
    assert om.unknown() == [] and gw.ledger.btc > 0


async def test_unknown_before_accept_is_resolved_to_rejected(tmp_path: Path) -> None:
    om, gw, _, _ = await _setup(tmp_path)
    gw.faults.next_place = "network_before_accept"
    r = await om.submit(_buy("u2"))
    assert r.state is OrderState.REJECTED and "not_found_after_unknown" in (r.error_code or "")
    assert gw.ledger.btc == 0


async def test_unknown_stays_unknown_when_lookup_fails_and_blocks_new_orders(
    tmp_path: Path,
) -> None:
    om, gw, repo, _ = await _setup(tmp_path)
    gw.faults.next_place = "network_after_accept"
    gw.faults.get_order_failures = 5
    r = await om.submit(_buy("u3"))
    assert r.state is OrderState.UNKNOWN and om.unknown() == [r]
    with pytest.raises(OrderBlockedError, match="UNKNOWN_ORDER_PENDING"):
        await om.submit(_buy("blocked"))
    # after a restart the UNKNOWN order is rehydrated and can be recovered
    om2 = OrderManager(gw, repo, EventBus(), TradingMode.PAPER, "KRW-BTC")
    await om2.load()
    assert [o.client_order_id for o in om2.unknown()] == ["u3"]
    gw.faults.get_order_failures = 0
    resolved = await om2.recover_unknown()
    assert resolved[0].state is OrderState.FILLED and om2.unknown() == []


async def test_partial_fill_then_cancel_reports_executed_volume(tmp_path: Path) -> None:
    om, gw, _, rec = await _setup(tmp_path)
    b = await om.submit(_buy("b"))
    gw.faults.next_place = "partial"
    s = await om.submit(_sell("s", b.executed_volume))
    assert s.state is OrderState.CANCELLED and 0 < s.executed_volume < b.executed_volume
    assert s.raw.get("partial") is True and rec.types[-1] is EventType.ORDER_PARTIALLY_FILLED


async def test_duplicate_identifier_at_exchange_resolves_by_lookup(tmp_path: Path) -> None:
    om, gw, _, _ = await _setup(tmp_path)
    await gw.place_market_buy(
        "ghost", Decimal(10000)
    )  # exchange has it, DB does not (crash scenario)
    r = await om.submit(_buy("ghost", 10000))
    assert r.state is OrderState.FILLED and gw.place_calls == 2  # second call raised Duplicate


async def test_mode_mismatch_refused(tmp_path: Path) -> None:
    om, _, _, _ = await _setup(tmp_path)
    live_intent = OrderIntent(
        "x", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(10000), None, None, TradingMode.LIVE
    )
    with pytest.raises(OrderBlockedError, match="MODE_MISMATCH"):
        await om.submit(live_intent)


def test_client_order_id_format() -> None:
    cid = new_client_order_id("20260924120000-abc123", 7, Side.BID)
    assert cid == "20260924120000-abc123-000007-bid" and len(cid) <= 64
