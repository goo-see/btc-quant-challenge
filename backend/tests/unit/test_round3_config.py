"""Round 3: pre-registered config — D0 replica hashes to the frozen D0 parameter hash."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.types import Timeframe
from app.research.round3.config import DEFAULT_CONFIG, load_config, parameter_hash
from app.strategy.trend_following import TrendFollowing4h, TrendFollowingBreakout

ROOT = Path(__file__).resolve().parents[3]


def test_config_loads_seven_fixed_hypotheses() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    ids = [s.strategy_id for s in cfg.strategies]
    assert ids == ["D0", "D1", "D2", "D3", "D4", "D5", "D6"]
    assert cfg.control == "D0"
    assert cfg.research_db.name == "research_round3.db" and cfg.market_db.name == "quant.db"
    assert cfg.research_db != cfg.market_db
    expected = {
        "D0": ("4h", 5, 30, 20, 14),
        "D1": ("2h", 5, 60, 40, 28),
        "D2": ("1h", 5, 120, 80, 56),
        "D3": ("2h", 3, 36, 24, 28),
        "D4": ("1h", 3, 72, 48, 56),
        "D5": ("2h", 2, 24, 16, 28),
        "D6": ("1h", 2, 48, 32, 56),
    }
    for s in cfg.strategies:
        tf, hz, en, ex, atr = expected[s.strategy_id]
        assert s.timeframe.value == tf and s.horizon_days == hz
        assert (s.entry_n, s.exit_n, int(s.params["atr_period"])) == (en, ex, atr)
        # breakout horizon in calendar time is exactly entry_n bars; ATR spans ~56h on every TF
        assert s.entry_n * s.timeframe.minutes / 1440 == hz
        assert int(s.params["atr_period"]) * s.timeframe.minutes == 56 * 60
        assert s.trend_timeframe is Timeframe.D1
        for k, v in {
            "trend_ema": 100,
            "atr_stop_mult": 3.0,
            "atr_trail_mult": 4.0,
            "exit_on_trend_loss": 1,
        }.items():
            assert s.params[k] == v


def test_d0_replica_hash_equals_frozen_d0_hash() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    frozen = yaml.safe_load((ROOT / "config/strategies/trend_following_4h_v1.yaml").read_text())
    frozen_params = {k: float(v) for k, v in frozen["params"].items()}
    d0 = cfg.strategy("D0")
    assert d0.params == frozen_params
    assert d0.parameter_hash == parameter_hash(frozen_params) == "c0e59de4f7b70c0c"
    assert d0.strategy_version == frozen["strategy_version"] == "trend_following_4h_v1"
    assert isinstance(d0.build(), TrendFollowing4h)  # same class the trader runs


def test_d1_to_d6_hashes_distinct_and_generic_class() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    hashes = {s.strategy_id: s.parameter_hash for s in cfg.strategies}
    assert len(set(hashes.values())) == 7
    for s in cfg.strategies[1:]:
        strat = s.build()
        assert type(strat) is TrendFollowingBreakout and strat.timeframe is s.timeframe
        assert strat.timeframes == [s.timeframe, Timeframe.D1]
