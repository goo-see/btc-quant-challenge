"""Strategy interface. Strategies ONLY emit Signals — they never touch orders or balances."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.types import Candle, Signal, Timeframe


@dataclass(frozen=True, slots=True)
class PositionView:
    quantity: Decimal
    avg_entry: Decimal
    entry_time: datetime
    stop_price: Decimal | None
    highest_since_entry: Decimal


@dataclass(slots=True)
class StrategyContext:
    indicators: dict[Timeframe, dict[str, float]] = field(default_factory=dict)
    position: PositionView | None = None


class BaseStrategy(ABC):
    name: str = "base"
    version: str = "1"

    def __init__(
        self, timeframe: Timeframe, trend_timeframe: Timeframe | None, params: dict[str, float]
    ) -> None:
        self.timeframe = timeframe
        self.trend_timeframe = trend_timeframe
        self.params = params
        self._last_signal: Signal | None = None

    @property
    def timeframes(self) -> list[Timeframe]:
        tfs = [self.timeframe]
        if self.trend_timeframe is not None and self.trend_timeframe is not self.timeframe:
            tfs.append(self.trend_timeframe)
        return tfs

    @abstractmethod
    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        """Called for every CLOSED candle of any subscribed timeframe. Return a Signal only on
        the strategy's own timeframe; return None otherwise."""

    def generate_signal(self) -> Signal | None:
        return self._last_signal

    def _emit(self, s: Signal) -> Signal:
        self._last_signal = s
        return s
