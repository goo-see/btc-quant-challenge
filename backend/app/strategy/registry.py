from __future__ import annotations

from app.core.types import Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.donchian_breakout import DonchianBreakoutStrategy
from app.strategy.trend_following import TrendFollowing1d, TrendFollowing4h
from app.strategy.trend_momentum import TrendMomentumStrategy
from app.strategy.trend_pullback import TrendPullbackStrategy
from app.strategy.volatility_breakout import VolatilityBreakoutStrategy

_REGISTRY: dict[str, type[BaseStrategy]] = {
    cls.name: cls
    for cls in (
        TrendMomentumStrategy,
        TrendPullbackStrategy,
        DonchianBreakoutStrategy,
        VolatilityBreakoutStrategy,
        TrendFollowing4h,
        TrendFollowing1d,
    )
}


def build_strategy(
    name: str, timeframe: str, trend_timeframe: str | None, params: dict[str, float]
) -> BaseStrategy:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"unknown strategy {name!r}; available: {sorted(_REGISTRY)}")
    return cls(
        Timeframe(timeframe), Timeframe(trend_timeframe) if trend_timeframe else None, params
    )
