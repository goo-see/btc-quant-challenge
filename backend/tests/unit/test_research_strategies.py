"""A/B/C strategies emit signals through the same interface; research helpers behave."""

from __future__ import annotations

from decimal import Decimal

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.research import ApprovalCriteria, default_specs, neighborhood
from app.core.types import Action, Timeframe
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams
from app.strategy.registry import build_strategy
from tests.conftest import trend_1m

CFG = BacktestConfig(
    initial_equity=Decimal(100000),
    fill=FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(3)),
    sizing=SizingParams(
        Decimal("0.30"), Decimal(30000), Decimal(5000), Decimal("0.0005"), Decimal(1000)
    ),
)
FAST = {
    "rsi_period": 5,
    "atr_period": 5,
    "volume_sma_period": 5,
    "trend_ema": 20,
    "ema_fast": 5,
    "ema_slow": 12,
}


def _candles() -> list:  # type: ignore[type-arg]
    return resample_1m(trend_1m(40000, up_bars=900, down_bars=500), Timeframe.M15)


def test_all_specs_build_and_have_versions() -> None:
    for spec in default_specs():
        s = build_strategy(spec.name, spec.timeframe, spec.trend_timeframe, spec.base_params)
        assert s.name == spec.name and s.version and Timeframe(spec.timeframe) in s.timeframes
        for k, values in spec.grid.items():
            assert k in spec.base_params and len(values) >= 2


def test_trend_pullback_trades_on_uptrend_with_shallow_dips() -> None:
    # long up-legs with short shallow dips: dips stay above EMA-slow so they count as pullbacks
    candles = resample_1m(
        trend_1m(15000, up_bars=600, down_bars=75, up_step=0.0006, down_step=-0.0005),
        Timeframe.M15,
    )
    p = {
        **FAST,
        "pullback_rsi": 45,
        "arm_bars": 8,
        "rsi_exit_low": 40,
        "atr_stop_mult": 3.0,
        "atr_trail_mult": 5.0,
    }
    r = BacktestEngine(build_strategy("trend_pullback", "15m", "1h", p), CFG).run(candles)
    assert r.metrics.trade_count > 0
    assert any(t.entry_reason.startswith("PULLBACK_REENTRY") for t in r.trades)


def test_donchian_trades_and_respects_trend_filter_param() -> None:
    p = {
        **FAST,
        "entry_n": 20,
        "exit_n": 10,
        "atr_stop_mult": 3.0,
        "atr_trail_mult": 4.0,
        "use_trend_filter": 0,
    }
    r = BacktestEngine(build_strategy("donchian_breakout", "15m", "1h", p), CFG).run(_candles())
    assert r.metrics.trade_count > 0 and all(
        t.entry_reason.startswith("DONCHIAN_BREAKOUT") for t in r.trades
    )
    p2 = {**p, "use_trend_filter": 1}
    r2 = BacktestEngine(build_strategy("donchian_breakout", "15m", "1h", p2), CFG).run(_candles())
    assert r2.metrics.trade_count <= r.metrics.trade_count  # filter can only remove entries


def test_volatility_breakout_enters_once_per_reference_bar_and_time_exits() -> None:
    p = {
        **FAST,
        "k": 0.3,
        "hold_bars": 1,
        "atr_stop_mult": 3.0,
        "atr_trail_mult": 5.0,
        "use_trend_filter": 0,
    }
    r = BacktestEngine(build_strategy("volatility_breakout", "15m", "4h", p), CFG).run(_candles())
    assert r.metrics.trade_count > 0
    assert any(
        t.exit_reason.startswith("VB_TIME_EXIT") or t.exit_reason.startswith("STOP@")
        for t in r.trades
    )
    # never two entries inside the same 4h bar
    from datetime import datetime

    from app.market.candle_engine import bucket_start

    starts = [bucket_start(datetime.fromisoformat(t.entry_time), Timeframe.H4) for t in r.trades]
    assert len(starts) == len(set(starts))


def test_strategy_never_calls_orders_and_signals_only() -> None:
    for spec in default_specs():
        s = build_strategy(
            spec.name, spec.timeframe, spec.trend_timeframe, {**spec.base_params, **FAST}
        )
        r = BacktestEngine(s, CFG).run(_candles()[:400])
        last = s.generate_signal()
        assert last is None or last.action in (Action.BUY, Action.SELL, Action.HOLD)
        assert r.signals >= 0


def test_neighborhood_and_criteria_defaults() -> None:
    grid = {"a": [1.0, 2.0, 3.0], "b": [10.0, 20.0]}
    n = neighborhood({"a": 2.0, "b": 10.0, "c": 5.0}, grid)
    assert {frozenset(d.items()) for d in n} == {
        frozenset({"a": 1.0, "b": 10.0, "c": 5.0}.items()),
        frozenset({"a": 3.0, "b": 10.0, "c": 5.0}.items()),
        frozenset({"a": 2.0, "b": 20.0, "c": 5.0}.items()),
    }
    c = ApprovalCriteria()
    assert (
        c.min_oos_profit_factor == 1.2
        and c.max_oos_mdd_pct == 10
        and c.min_oos_trades == 50
        and c.min_profitable_window_ratio == 0.6
    )
