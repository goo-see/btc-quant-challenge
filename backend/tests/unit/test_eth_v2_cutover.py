"""G0 0.75% -> AGGRESSIVE V2 0.50% cutover: legacy lots EXIT_ONLY with untouched targets, no new
G0 buys, legacy sells allowed, V2 lattice exactly 0.50% from the activation price, V2 sell target =
actual fill x 1.005 tick-rounded, compounding unit applies to V2 buys, no seed / no synthetic buy
at activation, mixed-lot persistence, duplicate prevention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.types import Side
from app.eth_grid.live_engine import LiveGridEngine, LiveGridState, live_params, policy_from_params
from app.exchange.upbit.tick_size import round_price
from tests.unit.test_eth_grid_live import _fill

P = live_params()
POL = policy_from_params(P, enabled=True)
T0 = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
D = Decimal
V2, GAP2, LEGACY = "AGGRESSIVE_V2_050", D("0.005"), "LEGACY_G0_075"
PX0 = D(3_660_000)


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def _g0_with_lots() -> LiveGridEngine:
    """Seeded G0 engine with one completed cycle (like production): 4 seed lots, realized > 0."""
    eng = LiveGridEngine(P, policy=POL)
    it = eng.next_intent(T0, PX0, enabled=True)
    assert it is not None
    _fill(eng, it, PX0, T0, "seed")
    eng.end_observation(T0, PX0)
    lot1 = min(eng.s.lots, key=lambda lot: lot.target)
    sell = eng.next_intent(_t(1), lot1.target, enabled=True)
    assert sell is not None and sell.lot_id == lot1.lot_id
    _fill(eng, sell, lot1.target, _t(1), "s1")
    eng.end_observation(_t(1), lot1.target)
    eng.update_sizing(_t(1), "STARTUP")
    assert len(eng.s.lots) == 4 and eng.s.realized > 0 and eng.s.strategy_epoch == "G0"
    return eng


def _cut(eng: LiveGridEngine, price: Decimal, ts: datetime | None = None) -> None:
    assert eng.cutover(ts or _t(2), price, V2, GAP2, LEGACY)


def test_cutover_marks_legacy_exit_only_and_preserves_entries_targets_and_books() -> None:
    eng = _g0_with_lots()
    before = {
        lot.lot_id: (lot.quantity, lot.buy_price, lot.cost, lot.target, lot.gap_pct)
        for lot in eng.s.lots
    }
    cash, realized, fees, inv = eng.s.cash, eng.s.realized, eng.s.fees, eng.s.inventory_qty
    act = D(3_686_000)
    _cut(eng, act)
    s = eng.s
    assert s.strategy_epoch == V2 and s.gap == GAP2 and s.anchor == act and s.last_price == act
    assert not s.level_blocked and not s.lots_bought_now
    assert all(lot.exit_only and lot.strategy_epoch == LEGACY for lot in s.lots)
    assert {
        lot.lot_id: (lot.quantity, lot.buy_price, lot.cost, lot.target, lot.gap_pct)
        for lot in s.lots
    } == before
    assert all(lot.gap_pct == D("0.0075") for lot in s.lots)
    assert (s.cash, s.realized, s.fees, s.inventory_qty) == (cash, realized, fees, inv)
    assert s.cutover_history[-1]["to_epoch"] == V2 and s.cutover_history[-1]["legacy_lots"] == 4
    assert any(e.kind == "CUTOVER" for e in eng.events)
    # idempotent
    assert not eng.cutover(_t(3), act, V2, GAP2, LEGACY)
    # compounding state untouched
    assert eng.current_unit == 5000 and eng.s.sizing_epoch == "C0"


def test_no_synthetic_buy_at_activation_and_v2_buy_only_after_real_crossing() -> None:
    eng = _g0_with_lots()
    act = D(3_686_000)
    _cut(eng, act)
    # the activation tick itself (same price) creates nothing
    assert eng.next_intent(_t(2), act, enabled=True) is None
    eng.end_observation(_t(2), act)
    # a tick ABOVE the activation price: no buy; a tick slightly below but above level -1: no buy
    assert eng.next_intent(_t(3), act + 5000, enabled=True) is None
    eng.end_observation(_t(3), act + 5000)
    lvl1 = eng.level_price(-1)
    # geometric lattice: adjacent levels differ by EXACTLY the 0.50 % gap (anchor / 1.005 below)
    assert lvl1 == (act / D("1.005")).quantize(D(1))
    for k in range(-5, 5):
        assert abs(eng.level_price(k + 1) / eng.level_price(k) - D("1.005")) < D("0.000001")
    assert eng.next_intent(_t(4), lvl1 + 1, enabled=True) is None
    eng.end_observation(_t(4), lvl1 + 1)
    # a real crossing of level -1 -> V2 BUY at the compounding unit
    it = eng.next_intent(_t(5), lvl1 - 1, enabled=True)
    assert (
        it is not None
        and it.kind == "GRID_BUY"
        and it.level == -1
        and it.krw == eng.current_unit == 5000
    )
    assert it.reason.startswith("LEVEL-1")


def test_v2_sell_target_is_actual_fill_times_1_005_tick_rounded_and_v2_lot_fields() -> None:
    eng = _g0_with_lots()
    act = D(3_686_000)
    _cut(eng, act)
    lvl1 = eng.level_price(-1)
    it = eng.next_intent(_t(5), lvl1 - 1, enabled=True)
    assert it is not None
    fill_px = (lvl1 - 1) * D("1.0003")  # real (slipped) fill price
    _fill(eng, it, fill_px, _t(5), "v2b1")
    lot = next(lot for lot in eng.s.lots if not lot.exit_only)
    assert lot.strategy_epoch == V2 and lot.gap_pct == GAP2 and lot.entry_unit_krw == 5000
    expected = round_price(lot.buy_price * D("1.005"), "up")
    assert lot.target == expected and expected % D(1000) == 0  # KRW tick for ~3.6M is ₩1,000
    assert lot.target >= lot.buy_price * D("1.005")
    assert (
        lot.target != (lvl1 * D("1.005")).quantize(D(1)) or fill_px == lvl1
    )  # fill-based, not level-based
    # legacy targets still the old 0.75 % ladder
    assert sorted(x.target for x in eng.s.lots if x.exit_only) == [
        (PX0 * D("1.0075") ** j).quantize(D(1)) for j in range(2, 6)
    ]


def test_legacy_sell_allowed_no_g0_buy_and_duplicate_prevention_in_v2() -> None:
    eng = _g0_with_lots()
    act = D(3_686_000)
    _cut(eng, act)
    eng.end_observation(_t(2), act)
    # legacy lot target reached -> SELL intent for the legacy lot (exit allowed)
    legacy = min((x for x in eng.s.lots if x.exit_only), key=lambda x: x.target)
    it = eng.next_intent(_t(3), legacy.target, enabled=True)
    assert it is not None and it.kind == "GRID_SELL" and it.lot_id == legacy.lot_id
    _fill(eng, it, legacy.target, _t(3), "ls")
    assert eng.cycles[-1].strategy_epoch == LEGACY and eng.cycles[-1].lot_id == legacy.lot_id
    eng.end_observation(_t(3), legacy.target)
    # price falls back through the OLD G0 anchor region: no G0 buy exists any more; only V2 levels
    # (the legacy seed lots at level_ref 3,660,000 must NOT block V2 levels)
    v2_levels = [eng.level_price(k) for k in range(-1, -4, -1)]
    assert all(abs(lp / act - 1) < D("0.02") for lp in v2_levels)
    px = v2_levels[1] - 1  # cross levels -1 and -2
    n = 0
    it = eng.next_intent(_t(4), px, enabled=True)
    while it is not None and it.kind == "GRID_BUY":
        assert it.krw == 5000 and it.level in (-1, -2)
        _fill(eng, it, px, _t(4), f"v2-{it.level}")
        n += 1
        it = eng.next_intent(_t(4), px, enabled=True)
    assert n == 2
    eng.end_observation(_t(4), px)
    # same levels crossed again while held -> no duplicate
    eng.next_intent(_t(5), act, enabled=True)
    eng.end_observation(_t(5), act)
    assert eng.next_intent(_t(6), px, enabled=True) is None
    assert all(x.strategy_epoch == V2 for x in eng.s.lots if not x.exit_only)
    assert len([x for x in eng.s.lots if x.exit_only]) == 3


def test_compounding_unit_applies_to_v2_buys() -> None:
    eng = _g0_with_lots()
    _cut(eng, D(3_686_000))
    eng.end_observation(_t(2), D(3_686_000))
    eng.s.realized = D(10_000)  # simulated realized profit -> unit 6,000 on the next boundary
    lvl1 = eng.level_price(-1)
    it = eng.next_intent(_t(3), lvl1 - 1, enabled=True)
    assert it is not None and it.krw == 6000 and eng.s.sizing_epoch == "C1"
    _fill(eng, it, lvl1 - 1, _t(3), "v2c1")
    lot = next(x for x in eng.s.lots if not x.exit_only)
    assert lot.entry_unit_krw == 6000 and lot.strategy_epoch == V2
    assert all(x.entry_unit_krw == 5000 for x in eng.s.lots if x.exit_only)


def test_mixed_legacy_v2_lots_persist_and_restore() -> None:
    eng = _g0_with_lots()
    _cut(eng, D(3_686_000))
    eng.end_observation(_t(2), D(3_686_000))
    lvl1 = eng.level_price(-1)
    it = eng.next_intent(_t(3), lvl1 - 1, enabled=True)
    assert it is not None
    _fill(eng, it, lvl1 - 1, _t(3), "v2p")
    d = eng.s.to_dict()
    back = LiveGridState.from_dict(d)
    assert back.to_dict() == d
    eng2 = LiveGridEngine(P, back, policy=POL)
    assert eng2.s.strategy_epoch == V2 and eng2.s.gap == GAP2 and eng2.s.anchor == D(3_686_000)
    assert len([x for x in eng2.s.lots if x.exit_only]) == 4 and len(eng2.active_lots()) == 1
    assert {x.lot_id: x.target for x in eng2.s.lots} == {x.lot_id: x.target for x in eng.s.lots}
    # legacy state without the new fields loads as G0 (cutover then applies once, at a real price)
    old = {k: v for k, v in d.items() if k not in ("strategy_epoch", "cutover_history")}
    for lot in old["lots"]:
        for k in ("strategy_epoch", "gap_pct", "exit_only"):
            lot.pop(k, None)
    eng3 = LiveGridEngine(P, LiveGridState.from_dict(old), policy=POL)
    assert eng3.s.strategy_epoch == "G0" and all(not x.exit_only for x in eng3.s.lots)


def test_cutover_never_seeds_or_orders() -> None:
    eng = _g0_with_lots()
    before = len(eng.s.lots)
    _cut(eng, D(3_686_000))
    # a fresh engine (never seeded) switched before its seed simply seeds under V2 later
    fresh = LiveGridEngine(P, policy=POL)
    assert fresh.cutover(T0, D(3_686_000), V2, GAP2, LEGACY) and fresh.s.anchor == 0
    it = fresh.next_intent(T0, D(3_686_000), enabled=True)
    assert it is not None and it.kind == "SEED"  # only on a never-seeded engine
    assert len(eng.s.lots) == before and eng.s.pending is None
    assert Side.BID is Side.BID  # keep the import meaningful
