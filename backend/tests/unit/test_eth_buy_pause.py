"""Operator pause of NEW buys: sells keep working, lots/targets/sizing untouched, persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.eth_grid.live_engine import (
    GridStatus,
    LiveGridEngine,
    LiveGridState,
    live_params,
    policy_from_params,
)
from tests.unit.test_eth_grid_live import _fill

P = live_params()
POL = policy_from_params(P, enabled=True)
T0 = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
REASON = "PAUSED_NEW_BUYS_INVENTORY_RISK"


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def _st(e: LiveGridEngine) -> GridStatus:
    return e.status


def _eng() -> LiveGridEngine:
    e = LiveGridEngine(P, policy=POL)
    it = e.next_intent(T0, Decimal(3_600_000), enabled=True)
    assert it is not None
    _fill(e, it, Decimal(3_600_000), T0, "seed")
    e.end_observation(T0, Decimal(3_600_000))
    e.update_sizing(T0, "STARTUP")
    return e


def test_pause_blocks_new_buys_keeps_sells_and_lots() -> None:
    e = _eng()
    before = {
        lot.lot_id: (lot.quantity, lot.target, lot.buy_price, lot.cost, lot.strategy_epoch)
        for lot in e.s.lots
    }
    assert e.set_buy_pause(_t(1), REASON, "user")
    assert _st(e) is GridStatus.BUY_PAUSED and e.buys_allowed(Decimal(3_600_000)) == (False, REASON)
    assert any(ev.kind == "BUY_PAUSED" for ev in e.events)
    # a real crossing of a buy level -> NO buy intent
    assert e.next_intent(_t(2), e.level_price(-1) - 1, enabled=True) is None
    e.end_observation(_t(2), e.level_price(-1) - 1)
    assert e.next_intent(_t(3), e.level_price(-3) - 1, enabled=True) is None
    e.end_observation(_t(3), e.level_price(-3) - 1)
    # sells at the ORIGINAL targets still happen
    tgt = min(lot.target for lot in e.s.lots)
    it = e.next_intent(_t(4), tgt, enabled=True)
    assert (
        it is not None
        and it.kind == "GRID_SELL"
        and it.quantity == next(lot.quantity for lot in e.s.lots if lot.target == tgt)
    )
    _fill(e, it, tgt, _t(4), "s1")
    assert e.cycles[-1].net_pnl > 0 and len(e.s.lots) == 4
    assert {
        lot.lot_id: (lot.quantity, lot.target, lot.buy_price, lot.cost, lot.strategy_epoch)
        for lot in e.s.lots
    } == {k: v for k, v in before.items() if k in {lot.lot_id for lot in e.s.lots}}
    # sizing state untouched by the pause; compounding still computes but creates no buy
    assert e.current_unit == 5000 and e.s.sizing_epoch == "C0"
    assert e.ladder(Decimal(3_590_000))["buys"] == []  # no buy levels shown while paused
    # idempotent; resume restores buys
    assert not e.set_buy_pause(_t(5), REASON, "user")
    assert e.set_buy_pause(_t(6), None, "user") and _st(e) is GridStatus.RUNNING
    assert e.buys_allowed(Decimal(3_600_000))[0]


def test_pause_persists_and_restores() -> None:
    e = _eng()
    e.set_buy_pause(_t(1), REASON, "user")
    d = e.s.to_dict()
    back = LiveGridState.from_dict(d)
    assert back.buy_pause_reason == REASON and back.buy_pause_at == _t(1) and back.to_dict() == d
    e2 = LiveGridEngine(P, back, policy=POL)
    assert _st(e2) is GridStatus.BUY_PAUSED
    assert e2.next_intent(_t(2), e2.level_price(-1) - 1, enabled=True) is None
    # legacy state without the field -> not paused
    d.pop("buy_pause_reason")
    d.pop("buy_pause_at")
    d.pop("buy_pause_by")
    assert LiveGridState.from_dict(d).buy_pause_reason is None
