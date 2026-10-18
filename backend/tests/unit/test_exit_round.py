"""Exit-round research isolation + semantics: E0 == BacktestEngine trade-for-trade; overlays behave
as specified; no import path to orders/exchange/trader; nothing production is modified."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.backtest.engine import BacktestEngine
from app.backtest.metrics import TradeRecord
from app.core.types import Timeframe
from app.market.candle_engine import resample_1m
from app.research.exit_round.engine import ExitResearchEngine, exit_rules, lock_price
from app.research.exit_round.harness import classify, match_trades, reversal_stats
from app.research.round3.runner import backtest_config
from app.strategy.trend_following import TrendFollowing4h
from tests.unit.test_round3_shadow_engine import T0, synthetic_1m

ROOT = Path(__file__).resolve().parents[3]
PARAMS = {
    k: float(v)
    for k, v in yaml.safe_load((ROOT / "config/strategies/trend_following_4h_v1.yaml").read_text())[
        "params"
    ].items()
}
CFG = backtest_config(100_000)


def _key(t: TradeRecord) -> tuple[str, str, str, str, str, str]:
    return (
        t.entry_time,
        t.exit_time,
        str(t.entry_price),
        str(t.exit_price),
        str(t.quantity),
        str(t.net_pnl),
    )


def test_e0_equals_production_backtest_engine() -> None:
    h1 = resample_1m(synthetic_1m(300, seed=11), Timeframe.H1)
    start_at = T0 + timedelta(days=60)
    bt = BacktestEngine(TrendFollowing4h(Timeframe.H4, Timeframe.D1, dict(PARAMS)), CFG).run(
        h1, start_at=start_at
    )
    ex = ExitResearchEngine(exit_rules()[0], PARAMS, CFG).run(h1, start_at=start_at)
    assert bt.metrics.trade_count >= 3
    assert [_key(t) for t in ex.trades] == [_key(t) for t in bt.trades]
    assert ex.metrics.ending_equity == bt.metrics.ending_equity
    assert ex.metrics.max_drawdown_pct == bt.metrics.max_drawdown_pct
    assert all(p.r_unit > 0 for p in ex.positions)


def test_entries_identical_across_rules_until_exits_diverge() -> None:
    h1 = resample_1m(synthetic_1m(300, seed=11), Timeframe.H1)
    res = {
        r.key: ExitResearchEngine(r, PARAMS, CFG).run(h1, start_at=T0 + timedelta(days=60))
        for r in exit_rules()
    }
    first = {
        k: (v.positions[0].entry_time, v.positions[0].entry_price, v.positions[0].initial_stop)
        for k, v in res.items()
        if v.positions
    }
    assert (
        len(set(first.values())) == 1
    )  # the first entry (before any exit difference) is identical everywhere
    # the trailing multiplier never touches the entry stop: initial_stop identical for the common first trade
    assert res["E2"].positions[0].initial_stop == res["E0"].positions[0].initial_stop


def test_lock_price_covers_fees_and_slippage() -> None:
    entry, r = Decimal(100_000_000), Decimal(3_000_000)
    fee, slip = Decimal("0.0005"), Decimal("0.0003")
    be = lock_price(entry, r, 0.0, fee, slip)
    qty = Decimal("0.0003")
    net = qty * be * (1 - slip) * (1 - fee) - qty * entry * (1 + fee)
    assert net >= 0 and net < qty * Decimal(2000)  # >= 0 and tight (within rounding)
    lk = lock_price(entry, r, 0.5, fee, slip)
    net_lock = qty * lk * (1 - slip) * (1 - fee) - qty * entry * (1 + fee)
    assert net_lock >= qty * r * Decimal("0.5")
    assert be > entry and lk > be


def test_overlays_raise_stop_or_take_partial_as_specified() -> None:
    h1 = resample_1m(synthetic_1m(400, seed=3), Timeframe.H1)
    start_at = T0 + timedelta(days=60)
    e0 = ExitResearchEngine(exit_rules()[0], PARAMS, CFG).run(h1, start_at=start_at)
    e1 = ExitResearchEngine(exit_rules()[1], PARAMS, CFG).run(h1, start_at=start_at)
    e3 = ExitResearchEngine(exit_rules()[3], PARAMS, CFG).run(h1, start_at=start_at)
    e4 = ExitResearchEngine(exit_rules()[4], PARAMS, CFG).run(h1, start_at=start_at)
    # E1: every position that reached +1R has the lock applied and never ends with a net loss below ~0
    reached = [
        p
        for p in e1.positions
        if (p.mfe_price - p.entry_price) / p.r_unit >= 1 and p.lock_applied_at is not None
    ]
    assert reached, "synthetic series should produce >= 1R excursions"
    for p in reached:
        if p.legs and p.legs[-1].reason.startswith("STOP"):
            assert p.net_pnl >= Decimal(
                -50
            )  # break-even fill (within 1-tick rounding of the lock price)
    # E3: exactly one partial leg of 50% per position that reached +2R, remainder exits by baseline rules
    parts = [p for p in e3.positions if p.partial_at is not None]
    assert parts
    for p in parts:
        partial_legs = [lg for lg in p.legs if lg.reason.startswith("PARTIAL_TP")]
        assert len(partial_legs) == 1
        assert abs(partial_legs[0].quantity - p.quantity * Decimal("0.5")) <= Decimal("0.00000002")
        assert p.closed
    # E4: lock at 1.5R leaves >= 0.5R on stop exits
    locked = [
        p
        for p in e4.positions
        if p.lock_applied_at is not None and p.legs and p.legs[-1].reason.startswith("STOP")
    ]
    for p in locked:
        assert p.net_pnl >= p.r_unit * p.quantity * Decimal("0.5") - Decimal(50)
    # E0 never locks or partials
    assert all(p.lock_applied_at is None and p.partial_at is None for p in e0.positions)


def test_reversal_stats_and_matching_and_classify_shapes() -> None:
    h1 = resample_1m(synthetic_1m(400, seed=3), Timeframe.H1)
    res = {
        r.key: ExitResearchEngine(r, PARAMS, CFG).run(h1, start_at=T0 + timedelta(days=60))
        for r in exit_rules()[:2]
    }
    pos = [p.to_dict() for p in res["E0"].positions]
    rv = reversal_stats(pos)
    assert rv["trades"] == len(pos) and rv["profit_to_loss"]["count"] <= rv["losing_trades"]
    assert rv["reached_1R_then_loss"]["count"] <= rv["meaningful_profit_to_loss_0_5R"]["count"]
    fake = {
        k: {
            "full_period": {
                "positions": [p.to_dict() for p in v.positions],
                "reversal": reversal_stats([p.to_dict() for p in v.positions]),
            }
        }
        for k, v in res.items()
    }
    m = match_trades(fake["E0"], fake["E1"])
    assert m["matched"] >= 1 and m["matched"] + m["e0_only"] == len(res["E0"].positions)
    base: dict[str, Any] = {
        "oos": {
            "max_drawdown_pct": 7.0,
            "profit_factor": 4.0,
            "expectancy": 1000.0,
            "profitable_windows": 13,
            "net_return_pct": 200.0,
        },
        "recent_2y": {"net_return_pct": 19.0},
        "cost_stress": {"2.0x": {"oos_return_pct": 190.0}},
        "full_period": {"reversal": {"meaningful_profit_to_loss_0_5R": {"count": 10}}},
    }
    good: dict[str, Any] = {
        "oos": {
            "max_drawdown_pct": 6.5,
            "profit_factor": 3.8,
            "expectancy": 950.0,
            "profitable_windows": 13,
            "net_return_pct": 190.0,
        },
        "recent_2y": {"net_return_pct": 18.0},
        "cost_stress": {"2.0x": {"oos_return_pct": 180.0}},
        "full_period": {"reversal": {"meaningful_profit_to_loss_0_5R": {"count": 5}}},
    }
    mm = {"top10_e0_winners": [{"e0_pnl": 100.0, "c_pnl": 95.0}]}
    assert classify(base, good, mm)[0] == "PROMISING"
    good_oos: dict[str, float] = {str(k): float(v) for k, v in good["oos"].items()}
    bad = {
        **good,
        "oos": {**good_oos, "profit_factor": 2.0, "expectancy": 400.0, "net_return_pct": 90.0},
    }
    assert classify(base, bad, mm)[0] == "REJECT"


def test_research_package_has_no_order_or_exchange_imports() -> None:
    forbidden = (
        "app.exchange",
        "app.execution.order_manager",
        "app.execution.paper_gateway",
        "app.risk.risk_manager",
        "app.services",
        "app.config.settings",
        "app.database",
        "httpx",
        "websockets",
        "urllib",
        "socket",
    )
    for f in [
        *(ROOT / "backend/app/research/exit_round").glob("*.py"),
        ROOT / "scripts/research_exit_round.py",
    ]:
        tree = ast.parse(f.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        bad = [m for m in mods if m.startswith(forbidden)]
        assert not bad, (f.name, bad)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not (
            {"create_order", "place_market_buy", "place_market_sell", "set_state", "upsert_candles"}
            & names
        ), f.name


def test_e2_rule_only_changes_trailing_multiplier() -> None:
    e2 = next(r for r in exit_rules() if r.key == "E2")
    assert e2.trail_mult == 3.0 and e2.lock_trigger_r is None and e2.partial_trigger_r is None
    eng = ExitResearchEngine(e2, PARAMS, CFG)
    assert eng.strategy.params["atr_trail_mult"] == 3.0
    assert (
        eng.strategy.params["atr_stop_mult"] == 3.0
        and eng.strategy.params["entry_n"] == 30
        and eng.strategy.params["exit_n"] == 20
    )
    assert isinstance(eng.strategy, TrendFollowing4h)
