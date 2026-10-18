"""Round 3: fixed-parameter WF harness — deterministic, identical windows, gate classification."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.backtest.walk_forward import run_walk_forward
from app.core.types import Timeframe
from app.market.candle_engine import resample_1m
from app.research.round3.config import DEFAULT_CONFIG, GateSpec, HistoricalSpec, load_config
from app.research.round3.historical import (
    classify,
    evaluate_strategy,
    run_fixed_walk_forward,
    timing_vs_control,
)
from app.research.round3.runner import backtest_config
from tests.unit.test_round3_shadow_engine import T0, synthetic_1m

GATE = GateSpec(0.0, 1.2, 10.0, 0.6, 50, 30, (1.5, 2.0), 0.0)


def test_fixed_wf_matches_registry_wf_for_d0() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy("D0")
    h1 = resample_1m(synthetic_1m(400), Timeframe.H1)
    bt = backtest_config(100_000)
    ours = run_fixed_walk_forward(sd, h1, bt, train_days=120, test_days=60, warmup_days=30)
    ref = run_walk_forward(
        candles=h1,
        strategy_name="trend_following_4h",
        timeframe="4h",
        trend_timeframe="1d",
        base_params=sd.params,
        grid={},
        cfg=bt,
        train_days=120,
        test_days=60,
        warmup_days=30,
        min_trades=0,
    )
    assert len(ours.windows) == len(ref.windows) >= 3
    assert ours.stitched_test_return_pct == ref.stitched_test_return_pct
    assert ours.stitched_test_mdd_pct == ref.stitched_test_mdd_pct
    assert [w.test_metrics for w in ours.windows] == [w.test_metrics for w in ref.windows]


def test_evaluate_is_deterministic_and_complete() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy("D4")
    h1 = resample_1m(synthetic_1m(420), Timeframe.H1)
    hist = HistoricalSpec(
        start=T0,
        end=T0 + timedelta(days=420),
        train_days=120,
        test_days=60,
        roll_days=60,
        warmup_days=30,
        cost_multipliers=(1.0, 1.5, 2.0),
        recent_2y_start=T0 + timedelta(days=300),
        ytd_start=T0 + timedelta(days=380),
        fake_breakout_max_hours=48,
    )
    a = evaluate_strategy(sd, h1, backtest_config(100_000), hist, GATE)
    b = evaluate_strategy(sd, h1, backtest_config(100_000), hist, GATE)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    for k in (
        "oos",
        "windows",
        "cost_stress",
        "concentration",
        "full_period",
        "recent_2y",
        "ytd_2026",
        "gate",
        "fake_breakout_full",
    ):
        assert k in a
    o = a["oos"]
    for k in (
        "net_return_pct",
        "max_drawdown_pct",
        "profit_factor",
        "trades",
        "win_rate",
        "expectancy",
        "exposure_pct",
        "fees",
        "profitable_windows",
        "losing_windows",
        "best_window",
        "worst_window",
    ):
        assert k in o
    assert set(a["cost_stress"]) == {"1.0x", "1.5x", "2.0x"}
    assert a["gate"]["verdict"] in {"PASS", "REVIEW", "REJECT"}
    assert a["recent_2y"]["available"] and a["ytd_2026"]["available"]
    t = timing_vs_control(a, a)
    assert t["d0_entries_matched"] == t["d0_entries"] == a["full_period"]["trades"]


def _m(**over: Any) -> dict[str, Any]:
    """Override keys as section__key; cost stress sections are addressed as stress15 / stress20."""
    base: dict[str, dict[str, Any]] = {
        "oos": {
            "net_return_pct": 50.0,
            "profit_factor": 2.0,
            "max_drawdown_pct": 5.0,
            "trades": 60,
            "profitable_windows": 10,
            "windows": 15,
        },
        "cost_stress": {"1.5x": {"oos_return_pct": 40.0}, "2.0x": {"oos_return_pct": 30.0}},
        "concentration": {"concentration_risk": False},
        "recent_2y": {"net_return_pct": 5.0},
    }
    alias = {"stress15": ("cost_stress", "1.5x"), "stress20": ("cost_stress", "2.0x")}
    for k, v in over.items():
        sect, key = k.split("__")
        if sect in alias:
            s_, k_ = alias[sect]
            base[s_][k_] = {key: v}
        else:
            base[sect][key] = v
    return base


def test_gate_classification_is_pre_registered() -> None:
    assert classify(_m(), GATE)[0] == "PASS"
    assert classify(_m(oos__trades=45), GATE)[0] == "REVIEW"  # <50 trades never PASS
    assert classify(_m(oos__trades=29), GATE)[0] == "REJECT"  # <30 trades -> insufficient evidence
    assert classify(_m(stress20__oos_return_pct=-1.0), GATE)[0] == "REVIEW"
    assert classify(_m(concentration__concentration_risk=True), GATE)[0] == "REVIEW"
    assert classify(_m(recent_2y__net_return_pct=-3.0), GATE)[0] == "REVIEW"
    assert classify(_m(oos__profit_factor=1.19), GATE)[0] == "REJECT"
    assert classify(_m(oos__max_drawdown_pct=10.01), GATE)[0] == "REJECT"
    assert classify(_m(oos__profitable_windows=8), GATE)[0] == "REJECT"  # 8/15 < 60%
    assert classify(_m(stress15__oos_return_pct=-0.1), GATE)[0] == "REJECT"
    assert classify(_m(oos__net_return_pct=0.0), GATE)[0] == "REJECT"
