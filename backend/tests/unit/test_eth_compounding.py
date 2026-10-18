"""Realized-profit compounding of the ETH grid order unit: sizing table, hard cap, step-up limit,
unrealized never counts, losses shrink immediately, open lots/targets immutable, legacy lots
unaffected, gap unchanged, inventory/cash ratios scale, no outside funds, persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config.settings import EthGridConfig
from app.core.types import Side
from app.eth_grid.compounding import (
    CompoundingPolicy,
    floor_krw,
    next_threshold,
    next_unit,
    projected_units,
    target_unit,
)
from app.eth_grid.live_engine import (
    GridStatus,
    Intent,
    LiveGridEngine,
    LiveGridState,
    live_params,
    policy_from_params,
)
from app.eth_grid.risk import GridRisk
from tests.unit.test_eth_grid_live import _ctx, _fill

P = live_params()
POL = policy_from_params(P, enabled=True)
T0 = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
D = Decimal


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


PX0 = Decimal(3_600_000)


def _seeded(policy: CompoundingPolicy | None = POL, price: Decimal = PX0) -> LiveGridEngine:
    eng = LiveGridEngine(P, policy=policy)
    it = eng.next_intent(T0, price, enabled=True)
    assert it is not None
    _fill(eng, it, price, T0, "seed")
    eng.end_observation(T0, price)
    return eng


def _st(eng: LiveGridEngine) -> GridStatus:
    return eng.status


def _paused(eng: LiveGridEngine) -> str | None:
    return eng.s.sizing_paused_reason


# ----------------------------------------------------------------------------- policy
def test_policy_ratios_come_from_the_frozen_candidate() -> None:
    assert POL.unit_ratio == D("0.1") and POL.max_inventory_ratio == D("0.8")
    assert POL.min_cash_ratio == D("0.2") and POL.max_single_order_krw == 20000
    assert POL.round_krw == 1000 and POL.max_step_up_pct == D("0.25") and not POL.auto_rebalance
    assert CompoundingPolicy.from_dict(POL.to_dict()) == POL


@pytest.mark.parametrize(
    ("capital", "unit"),
    [
        (50_000, 5_000),
        (57_430, 5_000),
        (59_999, 5_000),
        (60_000, 6_000),
        (62_800, 6_000),
        (75_000, 7_000),
        (100_000, 10_000),
        (150_000, 15_000),
        (200_000, 20_000),
        (250_000, 20_000),  # hard cap
        (1_000_000, 20_000),
        (49_999, 4_000),  # below minimum total -> paused by the engine
        (0, 0),
    ],
)
def test_target_unit_table_floor_1000_and_hard_cap(capital: int, unit: int) -> None:
    assert target_unit(D(capital), POL) == unit


def test_step_up_limited_to_25_percent_but_decrease_immediate() -> None:
    assert next_unit(D(100_000), D(5_000), POL) == 6_000  # 5,000 x 1.25 = 6,250 -> floor 6,000
    assert next_unit(D(100_000), D(6_000), POL) == 7_000  # 7,500 -> 7,000
    assert next_unit(D(100_000), D(8_000), POL) == 10_000  # target reached within the step
    assert next_unit(D(48_000), D(6_000), POL) == 4_000  # loss: immediate cut, no step limit
    assert next_unit(D(300_000), D(20_000), POL) == 20_000
    assert floor_krw(D(5_743), D(1000)) == 5000
    nxt, thr = next_threshold(D(5_000), POL)
    assert nxt == 6_000 and thr == 60_000
    assert next_threshold(D(20_000), POL) == (None, None)
    assert projected_units(POL)[-1] == {"capital": "200000", "unit": "20000"}


# ----------------------------------------------------------------------------- engine
def test_enabled_engine_starts_at_c0_with_the_research_unit() -> None:
    eng = _seeded()
    eng.update_sizing(_t(1), "STARTUP")
    s = eng.s
    assert eng.current_unit == 5000 and s.sizing_epoch == "C0" and s.compounding_capital == D(50000)
    assert s.sizing_history and s.sizing_history[0]["epoch"] == "C0"
    assert eng.s.gap == D("0.0075")  # grid gap untouched by compounding


def test_unrealized_profit_never_increases_unit_but_realized_does() -> None:
    eng = _seeded()
    eng.update_sizing(_t(1), "STARTUP")
    # ETH pumps +30 % while the seed lots are held -> big unrealized gain, nothing realized yet
    eng.s.realized = D(0)
    eng.next_intent(
        _t(2), D(4_700_000), enabled=True
    )  # sells would trigger; skip them via skip_lots
    hi = D(4_700_000)
    it = eng.next_intent(
        _t(2), hi, enabled=True, skip_lots=frozenset({lot.lot_id for lot in eng.s.lots})
    )
    assert it is None
    assert eng.s.unrealized(hi) > 5000 and eng.realized_capital() == D(50000)
    assert eng.update_sizing(_t(2), "TEST") == 5000 and eng.s.sizing_epoch == "C0"
    # realized +₩10,000 (net, fees included in realized) -> capital 60,000 -> unit 6,000 (within +25 %)
    eng.s.realized = D(10_000)
    assert eng.update_sizing(_t(3), "AFTER_CYCLE") == 6000
    assert eng.s.sizing_epoch == "C1" and eng.s.compounding_capital == D(60000)
    assert any(e.kind == "SIZING_UPDATE" for e in eng.events)
    # +₩50,000 realized -> target 10,000 but only +25 % per update: 6,000 -> 7,000 -> 8,000 ...
    eng.s.realized = D(50_000)
    assert eng.update_sizing(_t(4), "AFTER_CYCLE") == 7000 and eng.s.sizing_epoch == "C2"
    assert eng.update_sizing(_t(5), "AFTER_CYCLE") == 8000
    # realized loss -> unit cut immediately to the new target (no step limit downward)
    eng.s.realized = D(-2_000)
    assert eng.update_sizing(_t(6), "AFTER_CYCLE") == 4000
    assert _paused(eng) == "COMPOUNDING_CAPITAL_TOO_LOW" and _st(eng) is GridStatus.BUY_PAUSED
    assert eng.buys_allowed(D(3_600_000)) == (False, "COMPOUNDING_CAPITAL_TOO_LOW")
    eng.s.realized = D(0)
    assert eng.update_sizing(_t(7), "AFTER_CYCLE") == 5000 and _paused(eng) is None


def test_new_buy_uses_new_unit_but_open_lots_and_targets_are_immutable() -> None:
    eng = _seeded()
    eng.update_sizing(_t(0), "STARTUP")
    old_lots = {
        lot.lot_id: (lot.quantity, lot.target, lot.entry_unit_krw, lot.sizing_epoch)
        for lot in eng.s.lots
    }
    # a first grid buy at the fixed unit
    lvl1 = eng.level_price(-1)
    it = eng.next_intent(_t(1), lvl1 - 1, enabled=True)
    assert it is not None and it.krw == 5000
    _fill(eng, it, lvl1 - 1, _t(1), "b1")
    eng.end_observation(_t(1), lvl1 - 1)
    lot1 = next(lot for lot in eng.s.lots if lot.level == -1)
    assert lot1.entry_unit_krw == 5000 and lot1.sizing_epoch == "C0"
    # realized profit arrives (simulated) -> next NEW buy is ₩6,000; existing lots unchanged
    eng.s.realized = D(10_000)
    lvl2 = eng.level_price(-2)
    it2 = eng.next_intent(_t(2), lvl2 - 1, enabled=True)
    assert (
        it2 is not None
        and it2.kind == "GRID_BUY"
        and it2.krw == 6000
        and eng.s.sizing_epoch == "C1"
    )
    _fill(eng, it2, lvl2 - 1, _t(2), "b2")
    lot2 = next(lot for lot in eng.s.lots if lot.level == -2)
    assert lot2.entry_unit_krw == 6000 and lot2.sizing_epoch == "C1"
    assert abs(lot2.buy_funds - 6000) < 1
    for lot in eng.s.lots:
        if lot.lot_id in old_lots:
            assert (lot.quantity, lot.target, lot.entry_unit_krw, lot.sizing_epoch) == old_lots[
                lot.lot_id
            ]
    assert lot1.target == (lvl1 * D("1.0075")).quantize(D(1)) and lot1.entry_unit_krw == D(5000)

    # selling the old ₩5,000 lot closes at ITS unit and records the epoch on the cycle
    eng.end_observation(_t(2), lvl2 - 1)
    sell = eng.next_intent(_t(3), lot1.target, enabled=True, skip_lots=frozenset({lot2.lot_id}))
    assert sell is not None and sell.lot_id == lot1.lot_id
    _fill(eng, sell, lot1.target, _t(3), "s1")
    cyc = eng.cycles[-1]
    assert cyc.entry_unit_krw == 5000 and cyc.sizing_epoch == "C0"
    view = eng.compounding_view(lot1.target)
    assert (
        view["attribution"]["compounding_contribution"] is None
    )  # no larger-unit cycle closed yet
    assert view["enabled"] is True and view["auto_rebalance"] is False


def test_inventory_and_cash_limits_scale_with_ratios_on_the_conservative_base() -> None:
    eng = _seeded()
    eng.update_sizing(_t(0), "STARTUP")
    px = D(3_600_000)
    assert eng.effective_limits(px) == (D(40000), D(10000)) or eng.effective_limits(px)[0] <= 40000
    # capital grows to 100k realized but equity is only ~60k -> inventory cap uses the SMALLER base,
    # cash reserve uses the LARGER base (always conservative)
    eng.s.realized = D(50_000)
    eng.s.cash += D(50_000)  # the realized cash is in the sleeve
    eq = eng.s.equity(px)
    max_inv, min_cash = eng.effective_limits(px)
    cap = eng.realized_capital()
    assert max_inv == (min(cap, eq) * D("0.8")).quantize(D(1))
    assert min_cash == (max(cap, eq) * D("0.2")).quantize(D(1))
    assert max_inv <= D(80_000) and min_cash >= D(20_000)
    # disabled policy -> fixed research amounts
    plain = _seeded(policy=None)
    assert plain.effective_limits(px) == (D(40000), D(10000)) and plain.current_unit == 5000


def test_compounding_capital_never_includes_outside_funds() -> None:
    eng = _seeded()
    eng.update_sizing(_t(0), "STARTUP")
    # even if the sleeve cash were (wrongly) higher than the books, capital = 50,000 + realized only
    eng.s.cash += D(100_000)
    assert eng.realized_capital() == D(50000) and eng.update_sizing(_t(1), "TEST") == 5000
    eng.s.realized = D(25_000)
    assert eng.realized_capital() == D(75_000)


def test_legacy_lots_without_sizing_fields_load_as_c0_5000() -> None:
    eng = _seeded()
    d = eng.s.to_dict()
    for lot in d["lots"]:
        lot.pop("entry_unit_krw")
        lot.pop("sizing_epoch")
    for k in (
        "unit_krw",
        "compounding_capital",
        "sizing_epoch",
        "sizing_epoch_n",
        "sizing_history",
        "sizing_paused_reason",
        "compounding_enabled",
    ):
        d.pop(k, None)
    st = LiveGridState.from_dict(d)
    eng2 = LiveGridEngine(P, st, policy=POL)
    assert all(lot.entry_unit_krw == 5000 and lot.sizing_epoch == "C0" for lot in eng2.s.lots)
    assert [lot.target for lot in eng2.s.lots] == [lot.target for lot in eng.s.lots]
    assert eng2.update_sizing(_t(1), "STARTUP") == 5000 and eng2.s.sizing_epoch == "C0"
    # persistence round trip keeps sizing state
    eng2.s.realized = D(10_000)
    eng2.update_sizing(_t(2), "AFTER_CYCLE")
    back = LiveGridState.from_dict(eng2.s.to_dict())
    assert (
        back.unit_krw == 6000 and back.sizing_epoch == "C1" and back.compounding_capital == D(60000)
    )
    assert back.sizing_history[-1]["epoch"] == "C1"


def test_risk_layer_enforces_current_unit_and_hard_cap() -> None:
    risk = GridRisk(EthGridConfig())
    eng = _seeded()
    eng.update_sizing(_t(0), "STARTUP")
    eng.s.realized = D(10_000)
    eng.update_sizing(_t(1), "AFTER_CYCLE")
    ok = Intent("GRID_BUY", Side.BID, D(3_573_000), krw=D(6000), level=-1)
    assert risk.evaluate(ok, eng, _ctx()).approved
    stale = Intent("GRID_BUY", Side.BID, D(3_573_000), krw=D(5000), level=-1)
    assert risk.evaluate(stale, eng, _ctx()).reason == "AMOUNT_NOT_POLICY"
    eng.s.unit_krw = D(25_000)  # corrupted state: above the hard cap
    big = Intent("GRID_BUY", Side.BID, D(3_573_000), krw=D(25_000), level=-1)
    assert risk.evaluate(big, eng, _ctx()).reason == "MAX_SINGLE_ORDER"
