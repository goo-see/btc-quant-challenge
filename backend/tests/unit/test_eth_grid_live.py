"""ETH grid LIVE engine / risk / trend: real-fill accounting, parity with the research engine,
duplicate prevention, guards (inventory, cash, downtrend, MDD, daily loss), persistence, no direct
exchange calls outside OrderManager."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from app.config.settings import EthGridConfig
from app.core.types import Fill, OrderRecord, OrderState, OrdType, Side, TradingMode
from app.eth_grid.live_engine import (
    GridStatus,
    Intent,
    LiveGridEngine,
    LiveGridState,
    live_params,
)
from app.eth_grid.risk import GridRisk, GridRiskContext
from app.eth_grid.service import korean_event
from app.eth_grid.trend import DailyTrendGuard
from app.research.eth_grid.engine import GridEngine, grid_candidates

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
P = live_params()
Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")
FEE = P.fee_rate


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def _rec(
    cid: str,
    side: Side,
    *,
    qty: Decimal,
    price: Decimal,
    fee: Decimal | None = None,
    uuid: str = "u-1",
    state: OrderState = OrderState.FILLED,
    ts: datetime = T0,
) -> OrderRecord:
    funds = (qty * price).quantize(Q2)
    fee = fee if fee is not None else (funds * FEE).quantize(Q2)
    rec = OrderRecord(
        cid,
        TradingMode.LIVE,
        "KRW-ETH",
        side,
        OrdType.PRICE if side is Side.BID else OrdType.MARKET,
        state,
        ts,
        ts,
        req_krw=funds if side is Side.BID else None,
        req_volume=qty if side is Side.ASK else None,
        exchange_uuid=uuid,
        executed_volume=qty,
        avg_fill_price=price,
        paid_fee=fee,
    )
    rec.fills = [Fill(cid, price, qty, funds, fee, ts, f"t-{cid}")]
    return rec


def _fill(eng: LiveGridEngine, intent: Intent, px: Decimal, ts: datetime, cid: str) -> None:
    """Ideal exchange: market BUY of ₩krw at px, market SELL of qty at px."""
    eng.mark_pending(intent, cid, ts)
    if intent.side is Side.BID:
        assert intent.krw is not None
        qty = (intent.krw / px).quantize(Q8, rounding=ROUND_DOWN)
        eng.apply_buy(_rec(cid, Side.BID, qty=qty, price=px, ts=ts, uuid=f"uu-{cid}"), ts)
    else:
        assert intent.quantity is not None
        eng.apply_sell(
            _rec(cid, Side.ASK, qty=intent.quantity, price=px, ts=ts, uuid=f"uu-{cid}"), ts
        )


def _seeded(price: Decimal = Decimal(3_600_000)) -> LiveGridEngine:
    eng = LiveGridEngine(P)
    it = eng.next_intent(T0, price, enabled=True)
    assert it is not None and it.kind == "SEED" and it.krw == P.seed_krw
    _fill(eng, it, price, T0, "seed")
    eng.end_observation(T0, price)
    return eng


def _frozen(eng: LiveGridEngine) -> bool:  # defeats mypy narrowing on dataclass defaults
    return eng.s.frozen


def _halted(eng: LiveGridEngine) -> bool:
    return eng.s.halted


def _trend_ok(eng: LiveGridEngine) -> bool:
    return eng.s.trend_ok


def _st(eng: LiveGridEngine) -> GridStatus:
    return eng.status


def _paused(eng: LiveGridEngine) -> bool:
    return eng.s.day_paused


# ----------------------------------------------------------------------------- parameters
def test_live_params_are_the_research_g0_candidate() -> None:
    g0 = grid_candidates()[0]
    assert P == g0 and P.key == "G0"
    assert P.gap_pct == Decimal("0.0075") and P.unit_krw == 5000 and P.seed_krw == 25000
    assert P.budget_krw == 50000 and P.max_inventory_krw == 40000 and P.min_cash_krw == 10000
    assert P.daily_loss_pause_pct == Decimal("0.05") and P.mdd_halt_pct == Decimal("0.12")
    assert P.ema_period == 100 and P.levels_each_side == 10
    with pytest.raises(ValueError):
        LiveGridEngine(grid_candidates()[3])  # ATR-adaptive is not allowed live


# ----------------------------------------------------------------------------- seed
def test_waiting_enable_produces_no_intent_and_seed_split_matches_exchange_fill() -> None:
    eng = LiveGridEngine(P)
    assert _st(eng) is GridStatus.WAITING_ENABLE
    assert eng.next_intent(T0, Decimal(3_600_000), enabled=False) is None
    it = eng.next_intent(T0, Decimal(3_600_000), enabled=True)
    assert it is not None and it.kind == "SEED" and it.side is Side.BID and it.krw == 25000
    eng.mark_pending(it, "ethg-seed", T0)
    assert _st(eng) is GridStatus.SEEDING
    assert eng.next_intent(T0, Decimal(3_600_000), enabled=True) is None  # pending blocks
    # real fill: 0.00683412 ETH for ₩24,999.98 + fee ₩12.50 (odd numbers on purpose)
    rec = _rec(
        "ethg-seed",
        Side.BID,
        qty=Decimal("0.00683412"),
        price=Decimal("3658108.17"),
        fee=Decimal("12.50"),
    )
    lots = eng.apply_buy(rec, T0)
    s = eng.s
    assert len(lots) == 5 and all(lot.seed for lot in lots)
    assert sum((lot.quantity for lot in lots), Decimal(0)) == rec.executed_volume
    assert sum((lot.cost for lot in lots), Decimal(0)) == rec.fills[0].funds + Decimal("12.50")
    assert s.cash == Decimal(50000) - rec.fills[0].funds - Decimal("12.50")
    assert s.anchor == Decimal(3_600_000) and s.gap == Decimal("0.0075")
    assert [lot.target for lot in lots] == [
        (Decimal(3_600_000) * Decimal("1.0075") ** j).quantize(Decimal(1)) for j in range(1, 6)
    ]
    assert s.pending is None and s.buys == 1 and _st(eng) is GridStatus.RUNNING
    assert abs(s.equity(Decimal(3_600_000)) - Decimal(50000)) < Decimal(500)
    ev = {e.kind for e in eng.events}
    assert "SEED" in ev


# ----------------------------------------------------------------------------- pair accounting
def test_grid_buy_sell_pair_with_real_fee_and_slippage() -> None:
    eng = _seeded()
    lvl = eng.level_price(-1)
    it = eng.next_intent(_t(1), lvl - 1, enabled=True)
    assert it is not None and it.kind == "GRID_BUY" and it.level == -1 and it.krw == 5000
    fill_px = (lvl - 1) * Decimal("1.0003")  # adverse slippage on the market buy
    _fill(eng, it, fill_px, _t(1), "b1")
    eng.end_observation(_t(1), lvl - 1)
    lot = next(lot for lot in eng.s.lots if not lot.seed)
    assert lot.level == -1 and lot.target == (lvl * Decimal("1.0075")).quantize(Decimal(1))
    assert lot.buy_fee == (lot.buy_funds * FEE).quantize(Q2)
    assert -1 in eng.s.level_blocked
    # crossing the same level again must NOT buy again (duplicate prevention)
    assert eng.next_intent(_t(2), lvl + 500, enabled=True) is None
    eng.end_observation(_t(2), lvl + 500)
    assert eng.next_intent(_t(3), lvl - 1, enabled=True) is None
    eng.end_observation(_t(3), lvl - 1)
    # target reached -> SELL exactly the lot quantity
    sell = eng.next_intent(_t(4), lot.target, enabled=True)
    assert sell is not None and sell.kind == "GRID_SELL" and sell.lot_id == lot.lot_id
    assert sell.quantity == lot.quantity
    sell_px = lot.target * Decimal("0.9997")
    cash_before = eng.s.cash
    _fill(eng, sell, sell_px, _t(4), "s1")
    eng.end_observation(_t(4), lot.target)
    cyc = eng.cycles[-1]
    funds = (lot.quantity * sell_px).quantize(Q2)
    fee = (funds * FEE).quantize(Q2)
    assert cyc.net_pnl == funds - fee - lot.cost and cyc.net_pnl > 0
    assert cyc.gross_pnl == funds - lot.buy_funds
    assert cyc.buy_uuid == "uu-b1" and cyc.sell_uuid == "uu-s1"
    assert eng.s.cash == cash_before + funds - fee
    assert eng.s.realized == cyc.net_pnl and eng.s.completed_cycles == 1
    assert -1 not in eng.s.level_blocked and not [lot for lot in eng.s.lots if not lot.seed]
    book = eng.book(lot.target)
    assert Decimal(book["current_equity"]) == eng.s.equity(lot.target)
    assert book["completed_cycles"] == 1 and book["buy_count"] == 2 and book["sell_count"] == 1
    for k in (
        "starting_equity",
        "current_equity",
        "high_watermark",
        "mdd_pct",
        "realized_pnl",
        "unrealized_pnl",
        "total_fees",
        "gross_grid_profit",
        "net_profit",
        "completed_cycles",
        "buy_count",
        "sell_count",
        "turnover",
        "eth_inventory_qty",
        "krw_inventory",
        "avg_cycle_profit",
        "median_cycle_profit",
        "avg_cycle_duration_sec",
        "best_cycle",
        "worst_cycle",
    ):
        assert k in book


def test_same_observation_buy_never_sells_and_pending_is_exclusive() -> None:
    eng = _seeded()
    lvl = eng.level_price(-1)
    it = eng.next_intent(_t(1), lvl - 1, enabled=True)
    assert it is not None
    _fill(eng, it, lvl - 1, _t(1), "b1")
    lot = next(lot for lot in eng.s.lots if not lot.seed)
    # same observation, price already above the new lot's target: no sell of THAT lot
    nxt = eng.next_intent(_t(1), lot.target + 1, enabled=True)
    assert nxt is None or nxt.lot_id != lot.lot_id
    eng.end_observation(_t(1), lvl - 1)
    it2 = eng.next_intent(_t(2), lot.target + 1, enabled=True)
    assert it2 is not None and it2.kind == "GRID_SELL" and it2.lot_id == lot.lot_id
    eng.mark_pending(it2, "s1", _t(2))
    with pytest.raises(RuntimeError):
        eng.mark_pending(it2, "s1-dup", _t(2))
    assert eng.next_intent(_t(2), lot.target + 1, enabled=True) is None


# ----------------------------------------------------------------------------- guards
def test_max_inventory_and_min_cash_block_buys_but_sells_continue() -> None:
    eng = _seeded()
    px = Decimal(3_600_000)
    bought = 0
    for k in range(1, 12):
        px = eng.level_price(-k) - 1
        it = eng.next_intent(_t(k), px, enabled=True)
        while it is not None and it.kind == "GRID_BUY":
            _fill(eng, it, px, _t(k), f"b{k}-{it.level}")
            bought += 1
            it = eng.next_intent(_t(k), px, enabled=True)
        eng.end_observation(_t(k), px)
    s = eng.s
    assert s.inventory_value(px) <= P.max_inventory_krw * Decimal("1.02")
    assert s.cash >= P.min_cash_krw - Decimal(10)
    blocked = [e for e in eng.events if e.kind == "BUY_BLOCKED"]
    assert blocked and blocked[-1].detail["reason"] in ("MAX_INVENTORY", "MIN_CASH_RESERVE")
    assert bought >= 2
    n = len(s.lots)
    tgt = min(lot.target for lot in s.lots)
    it = eng.next_intent(_t(30), tgt, enabled=True)
    assert it is not None and it.kind == "GRID_SELL"  # sells still allowed
    _fill(eng, it, tgt, _t(30), "s-x")
    assert len(s.lots) == n - 1


def test_downtrend_guard_pauses_buys_allows_sells() -> None:
    eng = _seeded()
    eng.set_trend(_t(1), Decimal(3_500_000), Decimal(3_550_000))  # close < EMA100
    assert not _trend_ok(eng) and _st(eng) is GridStatus.BUY_PAUSED
    assert any(e.kind == "GUARD_ON" for e in eng.events)
    assert eng.next_intent(_t(2), eng.level_price(-1) - 1, enabled=True) is None
    assert any(
        e.kind == "BUY_BLOCKED" and e.detail["reason"] == "DOWNTREND_GUARD" for e in eng.events
    )
    eng.end_observation(_t(2), eng.level_price(-1) - 1)
    tgt = min(lot.target for lot in eng.s.lots)
    it = eng.next_intent(_t(3), tgt, enabled=True)
    assert it is not None and it.kind == "GRID_SELL"
    eng.set_trend(_t(4), Decimal(3_700_000), Decimal(3_550_000))
    assert _trend_ok(eng)


def test_mdd_halt_blocks_buys_allows_sells_and_milestones() -> None:
    eng = _seeded()
    # load inventory, then crash the price
    px = Decimal(3_600_000)
    for k in range(1, 4):
        px = eng.level_price(-k) - 1
        it = eng.next_intent(_t(k), px, enabled=True)
        while it is not None and it.kind == "GRID_BUY":
            _fill(eng, it, px, _t(k), f"b{k}")
            it = eng.next_intent(_t(k), px, enabled=True)
        eng.end_observation(_t(k), px)
    crash = Decimal(3_600_000) * Decimal("0.80")
    assert eng.next_intent(_t(10), crash, enabled=True) is None  # halted -> no buys
    assert _halted(eng) and _st(eng) is GridStatus.HALTED
    kinds = [e.kind for e in eng.events]
    assert "MDD_5" in kinds and "MDD_10" in kinds and "HALT" in kinds
    eng.end_observation(_t(10), crash)
    # sells of held lots still allowed once the price recovers to a target
    tgt = min(lot.target for lot in eng.s.lots)
    it = eng.next_intent(_t(11), tgt, enabled=True)
    assert it is not None and it.kind == "GRID_SELL"


def test_daily_loss_pause_resets_on_new_kst_day() -> None:
    eng = _seeded()
    px = Decimal(3_600_000)
    for k in range(1, 3):
        px = eng.level_price(-k) - 1
        it = eng.next_intent(_t(k), px, enabled=True)
        while it is not None and it.kind == "GRID_BUY":
            _fill(eng, it, px, _t(k), f"b{k}")
            it = eng.next_intent(_t(k), px, enabled=True)
        eng.end_observation(_t(k), px)
    drop = Decimal(3_600_000) * Decimal("0.90")  # -10% on ~35k inventory -> -7% sleeve day
    eng.next_intent(_t(5), drop, enabled=True)
    eng.end_observation(_t(5), drop)
    assert _paused(eng) and _st(eng) is GridStatus.BUY_PAUSED
    assert not _halted(eng)  # 7% < 12%
    nxt = T0 + timedelta(days=1)
    eng.next_intent(nxt, drop, enabled=True)
    assert not _paused(eng) and any(e.kind == "RESUME" for e in eng.events)


def test_recenter_preserves_lots_pnl_and_inventory() -> None:
    eng = _seeded()
    eng.set_trend(
        T0, Decimal(1), Decimal(2)
    )  # downtrend: no buys on the way down -> pure re-center
    inv, cash, realized = eng.s.inventory_qty, eng.s.cash, eng.s.realized
    far = eng.level_price(-P.levels_each_side) * Decimal("0.95")
    assert eng.next_intent(_t(1), far, enabled=True) is None
    assert eng.s.recenters == 1 and eng.s.anchor == far and not eng.s.level_blocked
    assert (eng.s.inventory_qty, eng.s.cash, eng.s.realized) == (inv, cash, realized)
    assert len(eng.s.lots) == 5  # lots and their targets untouched
    # far above: every seed target is crossed -> sells first (research order), then re-center
    eng2 = _seeded()
    hi = eng2.level_price(P.levels_each_side) * Decimal("1.05")
    it = eng2.next_intent(_t(1), hi, enabled=True)
    n = 0
    while it is not None and it.kind == "GRID_SELL":
        _fill(eng2, it, hi, _t(1), f"s{n}")
        n += 1
        it = eng2.next_intent(_t(1), hi, enabled=True)
    assert n == 5 and it is None and eng2.s.recenters == 1 and eng2.s.realized > 0


# ----------------------------------------------------------------------------- parity
def test_live_engine_matches_research_engine_on_a_price_path() -> None:
    """Same price path, zero slippage, ideal fills at the research fill prices -> identical BUY
    levels / SELL targets, cash, realized PnL and cycle count (grid arithmetic is shared)."""
    p0 = replace(P, slip=Decimal(0))
    research = GridEngine(p0)
    live = LiveGridEngine(p0)
    start = Decimal(3_600_000)
    research.on_price(T0, start)
    it = live.next_intent(T0, start, enabled=True)
    assert it is not None
    _fill(live, it, start, T0, "seed")
    live.end_observation(T0, start)
    path = [
        start * Decimal(1 + d / 10000)
        for d in (
            -40,
            -80,
            -120,
            -60,
            10,
            90,
            140,
            170,
            120,
            60,
            -20,
            -90,
            -160,
            -230,
            -300,
            -240,
            -150,
            -60,
            30,
            110,
            180,
            230,
            280,
            200,
            120,
            40,
            -40,
            -120,
            -200,
            -280,
            -360,
            -440,
            -380,
            -300,
            -220,
            -140,
            -60,
            20,
            100,
            180,
        )
    ]
    for i, px in enumerate(path, start=1):
        px = px.quantize(Decimal(1))
        prev_live = live.s.last_price
        research.on_price(_t(i), px)
        it = live.next_intent(_t(i), px, enabled=True)
        n = 0
        while it is not None and n < 20:
            n += 1
            if it.side is Side.BID:
                fill_px = live.level_price(it.level or 0)  # research buys at the level price
            else:
                lot = next(lot for lot in live.s.lots if lot.lot_id == it.lot_id)
                fill_px = lot.target if prev_live <= lot.target else px  # research sell rule
            _fill(live, it, fill_px, _t(i), f"o{i}-{n}")
            it = live.next_intent(_t(i), px, enabled=True)
        live.end_observation(_t(i), px)
    r, lv = research.s, live.s
    assert research.s.buys == lv.buys + 4  # research counts 5 seed units, live 1 seed order
    assert research.s.sells == lv.sells and len(research.cycles) == len(live.cycles)
    assert len(research.cycles) >= 2, "path should complete cycles"
    assert sorted(u.target for u in r.units) == sorted(lot.target for lot in lv.lots)
    assert abs(r.cash - lv.cash) < Decimal(2)
    assert abs(r.realized - lv.realized) < Decimal(2)
    assert abs(r.inventory_qty - lv.inventory_qty) < Decimal("0.0000005")
    assert r.anchor == lv.anchor and research._level_blocked == lv.level_blocked


# ----------------------------------------------------------------------------- persistence
def test_state_persistence_round_trip_including_pending() -> None:
    eng = _seeded()
    lvl = eng.level_price(-1)
    it = eng.next_intent(_t(1), lvl - 1, enabled=True)
    assert it is not None
    eng.mark_pending(it, "ethg-x", _t(1))
    eng.s.level_blocked.add(3)
    d = eng.s.to_dict()
    back = LiveGridState.from_dict(d)
    assert back.to_dict() == d
    assert back.pending is not None and back.pending.client_order_id == "ethg-x"
    assert back.level_blocked == {3} and len(back.lots) == 5 and back.cash == eng.s.cash
    eng2 = LiveGridEngine(P, back)
    assert _st(eng2) is GridStatus.RUNNING
    assert eng2.next_intent(_t(2), lvl - 1, enabled=True) is None  # pending still blocks
    rec = _rec("ethg-x", Side.BID, qty=Decimal("0.0013"), price=lvl - 1)
    eng2.apply_buy(rec, _t(2))
    assert eng2.s.pending is None and len(eng2.s.lots) == 6


def test_apply_buy_requires_matching_pending_and_rejected_clears() -> None:
    eng = _seeded()
    with pytest.raises(RuntimeError):
        eng.apply_buy(_rec("nope", Side.BID, qty=Decimal("0.001"), price=Decimal(3_600_000)), T0)
    it = eng.next_intent(_t(1), eng.level_price(-1) - 1, enabled=True)
    assert it is not None
    eng.mark_pending(it, "ethg-r", _t(1))
    rec = _rec(
        "ethg-r", Side.BID, qty=Decimal(0), price=Decimal(3_600_000), state=OrderState.REJECTED
    )
    assert eng.apply_buy(rec, _t(1)) == [] and eng.s.pending is None
    assert any(e.kind == "ORDER_NOT_FILLED" for e in eng.events)
    assert len(eng.s.lots) == 5 and -1 not in eng.s.level_blocked


# ----------------------------------------------------------------------------- risk layer
def _ctx(**kw: object) -> GridRiskContext:
    base: dict[str, object] = {
        "now": T0,
        "price": Decimal(3_600_000),
        "data_age_sec": 1.0,
        "inflight_orders": 0,
        "unknown_orders": 0,
        "orders_last_hour": 0,
        "orders_today": 0,
        "exchange_krw": Decimal(70000),
        "exchange_eth": Decimal("0.0069"),
    }
    base.update(kw)
    return GridRiskContext(**base)  # type: ignore[arg-type]


def test_risk_layer_execution_safety() -> None:
    cfg = EthGridConfig()
    risk = GridRisk(cfg)
    eng = _seeded()
    buy = Intent("GRID_BUY", Side.BID, Decimal(3_573_000), krw=Decimal(5000), level=-1)
    assert risk.evaluate(buy, eng, _ctx()).approved
    assert risk.evaluate(buy, eng, _ctx(data_age_sec=500.0)).reason == "STALE_MARKET_DATA"
    assert risk.evaluate(buy, eng, _ctx(data_age_sec=None)).reason == "STALE_MARKET_DATA"
    assert risk.evaluate(buy, eng, _ctx(inflight_orders=1)).reason == "INFLIGHT_ORDER"
    assert risk.evaluate(buy, eng, _ctx(unknown_orders=1)).reason == "UNKNOWN_ORDER_PENDING"
    assert risk.evaluate(buy, eng, _ctx(orders_last_hour=30)).reason == "ORDERS_PER_HOUR_GUARD"
    assert risk.evaluate(buy, eng, _ctx(orders_today=60)).reason == "ORDERS_PER_DAY_GUARD"
    assert "ORDERS_PER_HOUR_GUARD" in GridRisk.FREEZE_REASONS
    bad_amt = Intent("GRID_BUY", Side.BID, Decimal(3_573_000), krw=Decimal(7000), level=-1)
    assert risk.evaluate(bad_amt, eng, _ctx()).reason == "AMOUNT_NOT_POLICY"
    assert (
        risk.evaluate(buy, eng, _ctx(exchange_krw=Decimal(4000))).reason
        == "EXCHANGE_KRW_INSUFFICIENT"
    )
    eng.s.cash = Decimal(12000)  # 12,000 - 5,002.5 < 10,000 reserve
    assert risk.evaluate(buy, eng, _ctx()).reason == "MIN_CASH_RESERVE"
    eng.s.cash = Decimal(24000)
    eng.freeze(T0, "TEST")
    assert risk.evaluate(buy, eng, _ctx()).reason == "FROZEN_NO_AUTO_EXECUTION"
    eng.unfreeze(T0, "test")
    lot = eng.s.lots[0]
    sell = Intent("GRID_SELL", Side.ASK, lot.target, quantity=lot.quantity, lot_id=lot.lot_id)
    assert risk.evaluate(sell, eng, _ctx(price=lot.target)).approved
    low = Decimal(900_000)  # 0.0013 ETH * 900k = ~1,200 < 5,000 min total
    assert risk.evaluate(sell, eng, _ctx(price=low)).reason == "MIN_ORDER_SELL"
    too_much = Intent(
        "GRID_SELL", Side.ASK, lot.target, quantity=lot.quantity * 2, lot_id=lot.lot_id
    )
    assert risk.evaluate(too_much, eng, _ctx(price=lot.target)).reason == "SELL_EXCEEDS_LOT"
    assert (
        risk.evaluate(sell, eng, _ctx(price=lot.target, exchange_eth=Decimal(0))).reason
        == "EXCHANGE_ETH_INSUFFICIENT"
    )
    seed = Intent("SEED", Side.BID, Decimal(3_600_000), krw=Decimal(25000))
    assert risk.evaluate(seed, eng, _ctx()).reason == "ALREADY_SEEDED"
    fresh = LiveGridEngine(P)
    assert risk.evaluate(seed, fresh, _ctx()).approved
    assert (
        risk.evaluate(seed, fresh, _ctx(exchange_krw=Decimal(20000))).reason
        == "EXCHANGE_KRW_INSUFFICIENT"
    )


# ----------------------------------------------------------------------------- trend guard
def test_trend_guard_uses_complete_days_only_and_matches_ema_rule() -> None:
    g = DailyTrendGuard(3)
    day0 = datetime(2026, 10, 1, tzinfo=UTC)
    bars = []
    for d in range(4):  # 4 days x 6 bars (4h)
        for h in range(0, 24, 4):
            bars.append((day0 + timedelta(days=d, hours=h), Decimal(100 + d * 10 + h)))
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)  # day 4 (Oct 4) still running
    n = g.add_4h_candles(bars, now)
    assert n == 3 and max(g.closes) == datetime(2026, 10, 3).date()
    r = g.evaluate()
    assert r.days == 3 and r.daily_close == Decimal(140)  # Oct 3: 100+20+20
    assert r.ema == Decimal(str(round((120 + 130 + 140) / 3, 2))) and r.ok
    # a bar that is not the 20:00 bar never counts as a daily close
    g2 = DailyTrendGuard(3)
    assert g2.add_4h_candles([(day0, Decimal(1))], now) == 0
    # falling series -> not ok
    g3 = DailyTrendGuard(2)
    g3.closes = {datetime(2026, 1, i).date(): Decimal(200 - i * 10) for i in range(1, 6)}
    assert not g3.evaluate().ok


def test_korean_events_have_no_raw_enum_titles() -> None:
    from app.eth_grid.live_engine import GridEvent

    ev = GridEvent(
        T0,
        "SELL",
        {"funds": "5036.12", "fill": "3663000", "net": "31.2", "cycles": 3},
    )
    typ, _lvl, msg = korean_event(ev)
    assert typ == "ETH_GRID_SELL" and "순익 +₩31" in msg and "Cycle #3" in msg
    typ, _, msg = korean_event(GridEvent(T0, "FROZEN", {"reason": "API_ERRORS"}))
    assert typ == "ETH_FROZEN" and "FROZEN" in msg and "사용자 확인" in msg


# ----------------------------------------------------------------------------- order path hygiene
def test_grid_process_never_calls_exchange_order_endpoints_directly() -> None:
    forbidden_names = {"create_order", "place_market_buy", "place_market_sell", "test_order"}
    files = [*(ROOT / "backend/app/eth_grid").glob("*.py"), ROOT / "backend/app/main_eth_grid.py"]
    for f in files:
        tree = ast.parse(f.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (forbidden_names & names), (f.name, forbidden_names & names)
    # orders go through OrderManager.submit only
    svc = (ROOT / "backend/app/eth_grid/service.py").read_text()
    assert "self.orders.submit(" in svc and "live_gateway import" not in svc
    plist = (ROOT / "scripts/launchd/com.quant.eth-grid.plist").read_text()
    assert "app.main_eth_grid" in plist and "__TRADING_MODE__" in plist
    assert "main_trader" not in plist
