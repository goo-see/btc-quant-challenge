from __future__ import annotations

from decimal import Decimal

from app.core.types import Action, Timeframe
from app.market.candle_engine import resample_1m
from app.market.indicators import IndicatorSet
from app.strategy.base import PositionView, StrategyContext
from app.strategy.registry import build_strategy
from app.strategy.trend_momentum import TrendMomentumStrategy
from tests.conftest import trend_1m

PARAMS: dict[str, float] = {
    "ema_fast": 5,
    "ema_slow": 12,
    "trend_ema": 10,
    "cross_lookback": 3,
    "rsi_period": 5,
    "rsi_buy_min": 40,
    "rsi_buy_max": 90,
    "atr_period": 5,
    "atr_stop_mult": 2.0,
    "atr_trail_mult": 3.0,
    "volume_sma_period": 5,
    "volume_min_ratio": 0.0,
}


def _run(strategy: TrendMomentumStrategy, n_min: int = 3000) -> list[tuple[Action, str]]:
    candles = trend_1m(n_min, up_bars=400, down_bars=300)
    m15 = resample_1m(candles, Timeframe.M15)
    h1 = resample_1m(candles, Timeframe.H1)
    ind = {
        Timeframe.M15: IndicatorSet.from_params(PARAMS),
        Timeframe.H1: IndicatorSet.from_params(PARAMS),
    }
    ctx = StrategyContext()
    out: list[tuple[Action, str]] = []
    in_pos = False
    merged = sorted([*m15, *h1], key=lambda c: (c.close_time, -c.timeframe.minutes))
    for c in merged:
        ctx.indicators[c.timeframe] = ind[c.timeframe].update(c)
        ctx.position = (
            PositionView(Decimal("0.0003"), c.close, c.open_time, None, c.high) if in_pos else None
        )
        s = strategy.on_candle(c, ctx)
        if s is None:
            assert c.timeframe is Timeframe.H1  # trend TF never emits
            continue
        assert c.timeframe is Timeframe.M15
        out.append((s.action, s.reason))
        if s.action is Action.BUY:
            in_pos = True
            assert s.stop_price is not None and s.stop_price < c.close
            assert 0.0 <= s.strength <= 1.0
        elif s.action is Action.SELL:
            in_pos = False
    return out


def test_registry_builds_strategy() -> None:
    s = build_strategy("trend_momentum", "15m", "1h", PARAMS)
    assert isinstance(s, TrendMomentumStrategy)
    assert s.timeframes == [Timeframe.M15, Timeframe.H1]


def test_emits_buy_and_sell_on_sawtooth() -> None:
    s = build_strategy("trend_momentum", "15m", "1h", PARAMS)
    assert isinstance(s, TrendMomentumStrategy)
    out = _run(s)
    actions = [a for a, _ in out]
    assert Action.BUY in actions and Action.SELL in actions
    assert out[0][1] == "WARMUP"
    # SELL reasons are trend exits; every BUY is preceded by a flat state
    assert all(r == "EMA_DEATH_CROSS" for a, r in out if a is Action.SELL)


def test_trend_filter_blocks_buy_when_below_trend_ema() -> None:
    s = build_strategy("trend_momentum", "15m", "1h", {**PARAMS, "trend_ema": 3})
    assert isinstance(s, TrendMomentumStrategy)
    reasons = {r for a, r in _run(s) if a is Action.HOLD}
    assert "TREND_DOWN" in reasons


def test_hold_in_position_ratchets_stop_up_only() -> None:
    s = build_strategy("trend_momentum", "15m", None, PARAMS)
    assert isinstance(s, TrendMomentumStrategy)
    candles = resample_1m(trend_1m(6000, up_bars=6000, down_bars=0), Timeframe.M15)
    ind = IndicatorSet.from_params(PARAMS)
    ctx = StrategyContext()
    last_stop: Decimal | None = None
    for c in candles:
        ctx.indicators[Timeframe.M15] = ind.update(c)
        ctx.position = PositionView(Decimal("0.0003"), c.close, c.open_time, last_stop, c.high)
        sig = s.on_candle(c, ctx)
        assert sig is not None
        if sig.reason == "IN_POSITION_TRAIL":
            assert sig.stop_price is not None
            if last_stop is not None:
                assert sig.stop_price >= last_stop
            last_stop = sig.stop_price
    assert last_stop is not None
