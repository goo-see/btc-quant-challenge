"""ETH grid engine: pair accounting, min order/fee/slippage, duplicate suppression, inventory cap,
cash reserve, downtrend pause (sells allowed), daily-loss pause, MDD halt, re-center preserves PnL,
persistence round-trip, no order path."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.research.eth_grid.engine import GridEngine, GridParams, grid_candidates

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
P = grid_candidates()[1]  # G1 1.0%


def _halted(e: GridEngine) -> bool:  # defeats mypy narrowing on the dataclass default
    return e.s.halted


def _trend_ok(e: GridEngine) -> bool:
    return e.s.trend_ok


def _eng(price: int = 3_600_000, p: GridParams = P) -> GridEngine:
    e = GridEngine(p)
    e.on_price(T0, Decimal(price))  # seeds
    return e


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def test_seed_accounting_and_min_unit() -> None:
    e = _eng()
    s = e.s
    assert len(s.units) == 5 and all(u.seed for u in s.units)
    assert P.unit_krw >= 5000  # Upbit min order
    spent = sum((u.cost for u in s.units), Decimal(0))
    assert abs(spent - Decimal(25000) * (1 + P.fee_rate)) < Decimal(
        60
    )  # slippage changes qty, not spend
    assert s.cash == P.budget_krw - spent
    assert abs(e.s.equity(Decimal(3_600_000)) - Decimal(50000)) < Decimal(
        60
    )  # seed costs ≈ slip+fee only
    assert sorted(u.target for u in s.units) == [
        Decimal(round(3_600_000 * 1.01**j)) for j in range(1, 6)
    ]


def test_grid_buy_sell_pair_with_fee_and_slippage() -> None:
    e = _eng()
    lvl = e.level_price(-1)
    e.on_price(_t(1), lvl - 1)  # cross down through level -1 -> BUY one unit
    bought = [u for u in e.s.units if not u.seed]
    assert len(bought) == 1 and bought[0].level == -1
    u = bought[0]
    assert u.entry_price == lvl * (1 + P.slip)
    assert u.target == Decimal(round(lvl * Decimal("1.01")))
    e.on_price(_t(2), u.target)  # cross up through target -> SELL that unit
    assert not [x for x in e.s.units if not x.seed]
    c = e.cycles[-1]
    assert c.uid == u.uid and c.net > 0
    expected_net = (u.quantity * u.target * (1 - P.slip)) * (1 - P.fee_rate) - u.cost
    assert abs(c.net - expected_net) < Decimal("0.05")
    assert e.s.fees > 0


def test_duplicate_level_suppressed_and_same_observation_no_sell() -> None:
    e = _eng()
    lvl = e.level_price(-1)
    e.on_price(_t(1), lvl - 1)
    e.on_price(_t(2), lvl + 500)
    e.on_price(_t(3), lvl - 1)  # crossing the same level again while the unit is still held
    assert len([u for u in e.s.units if u.level == -1]) == 1
    # a unit bought on an observation never sells on the same observation (bar feed guards it)
    e2 = _eng()
    e2.on_bar(
        _t(10), Decimal(3_600_000), Decimal(3_700_000), Decimal(3_560_000), Decimal(3_690_000), 5
    )
    bought = [u for u in e2.s.units if not u.seed]
    assert bought and all(u.uid not in {c.uid for c in e2.cycles} for u in bought)


def test_max_inventory_and_min_cash_reserve_block_buys_but_sells_continue() -> None:
    from dataclasses import replace

    e = _eng(p=replace(P, daily_loss_pause_pct=Decimal("0.99"), mdd_halt_pct=Decimal("0.99")))
    px = Decimal(3_600_000)
    for k in range(1, 12):  # keep crossing lower levels
        px = e.level_price(-k) - 1
        e.on_price(_t(k), px)
    inv = e.s.inventory_value(px)
    assert inv <= P.max_inventory_krw * Decimal("1.02")
    assert e.s.cash >= P.min_cash_krw - Decimal(10)
    blocked = [ev for ev in e.events if ev.kind == "BUY_BLOCKED"]
    assert blocked and blocked[-1].detail["reason"] in ("MAX_INVENTORY", "MIN_CASH_RESERVE")
    before = len(e.s.units)
    e.on_price(_t(20), min(u.target for u in e.s.units))  # a sell still happens
    assert len(e.s.units) == before - 1


def test_downtrend_guard_pauses_buys_allows_sells() -> None:
    e = _eng()
    e.set_trend(_t(1), Decimal(3_500_000), Decimal(3_550_000))  # daily close < EMA100 -> guard on
    assert not _trend_ok(e) and any(ev.kind == "GUARD_ON" for ev in e.events)
    e.on_price(_t(2), e.level_price(-1) - 1)
    assert not [u for u in e.s.units if not u.seed]
    assert any(
        ev.kind == "BUY_BLOCKED" and ev.detail["reason"] == "DOWNTREND_GUARD" for ev in e.events
    )
    n = len(e.s.units)
    e.on_price(_t(3), min(u.target for u in e.s.units))
    assert len(e.s.units) == n - 1  # existing inventory still sells
    e.set_trend(_t(4), Decimal(3_700_000), Decimal(3_550_000))
    assert _trend_ok(e)
    e.on_price(_t(5), e.level_price(-2) - 1)
    assert [u for u in e.s.units if not u.seed]  # buys resume


def test_daily_loss_pause_and_mdd_halt() -> None:
    e = _eng()
    e.on_price(
        _t(1), Decimal(3_400_000)
    )  # −5.6% on 25k inventory ≈ −1.4k = −2.8% equity... push lower
    e.on_price(_t(2), Decimal(3_200_000))  # inventory ≈ −11% -> equity ≈ −5.6%
    assert e.s.day_paused and any(ev.kind == "PAUSE" for ev in e.events)
    e.on_price(_t(3), e.level_price(-10) + 1)
    e.on_price(_t(4), Decimal(2_500_000))  # deep drop -> MDD >= 12% -> HALT
    assert _halted(e)
    assert str(e.s.halt_reason).startswith("MDD")
    ok, why = e.buys_allowed(Decimal(2_500_000))
    assert not ok and why == "HALTED"
    # halted engine still sells inventory into a recovery
    n = len(e.s.units)
    e.on_price(_t(5), max(u.target for u in e.s.units) + 1)
    assert len(e.s.units) < n


def test_recenter_preserves_units_pnl_and_inventory() -> None:
    e = _eng()
    e.on_price(_t(1), e.level_price(-1) - 1)
    realized, fees, units, cash = e.s.realized, e.s.fees, list(e.s.units), e.s.cash
    far = e.level_price(-10) * Decimal("0.98")
    e.on_price(_t(2), far)  # outside the lattice -> re-center
    assert e.s.recenters == 1 and e.s.anchor == far
    assert e.s.realized == realized and e.s.fees == fees and e.s.cash == cash
    assert [u.uid for u in e.s.units] == [u.uid for u in units]  # inventory untouched
    assert all(
        u.target == v.target for u, v in zip(e.s.units, units, strict=True)
    )  # targets unchanged


def test_persistence_round_trip() -> None:
    e = _eng()
    e.set_trend(_t(1), Decimal(3_700_000), Decimal(3_500_000))
    e.on_price(_t(2), e.level_price(-1) - 1)
    e.on_price(_t(3), e.level_price(-2) - 1)
    e.on_price(_t(4), min(u.target for u in e.s.units))
    d = e.to_json()
    r = GridEngine.from_json(P, d)
    assert r.s.cash == e.s.cash and r.s.realized == e.s.realized and r.s.fees == e.s.fees
    assert [(u.uid, u.entry_price, u.target, u.level) for u in r.s.units] == [
        (u.uid, u.entry_price, u.target, u.level) for u in e.s.units
    ]
    assert (
        r.s.anchor == e.s.anchor
        and r.s.gap == e.s.gap
        and r.s.hwm == e.s.hwm
        and r.s.mdd == e.s.mdd
    )
    assert r.s.buys == e.s.buys and r.s.sells == e.s.sells and r.s.trend_ok == e.s.trend_ok
    # continues identically
    nxt = min(u.target for u in e.s.units)
    e.on_price(_t(5), nxt)
    r.on_price(_t(5), nxt)
    assert r.s.cash == e.s.cash and len(r.s.units) == len(e.s.units)


def test_atr_adaptive_gap_is_clamped() -> None:
    g3 = grid_candidates()[3]
    e = GridEngine(g3)
    e.set_gap(Decimal("0.001"))
    assert e.s.gap == g3.atr_min_pct
    e.set_gap(Decimal("0.05"))
    assert e.s.gap == g3.atr_max_pct
    e.set_gap(Decimal("0.012"))
    assert e.s.gap == Decimal("0.012")


def test_no_order_path_in_grid_package() -> None:
    forbidden = (
        "app.exchange.upbit.rest_client",
        "app.exchange.upbit.live_gateway",
        "app.exchange.upbit.auth",
        "app.execution",
        "app.risk",
        "app.services",
        "app.config.settings",
        "app.database",
        "httpx",
        "requests",
    )
    for f in (ROOT / "backend/app/research/eth_grid").glob("*.py"):
        tree = ast.parse(f.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not [m for m in mods if m.startswith(forbidden)], (f.name, mods)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not (
            {
                "create_order",
                "cancel_order",
                "place_market_buy",
                "place_market_sell",
                "OrderManager",
            }
            & names
        ), f.name
