"""Strategy C — Volatility breakout (Larry Williams style) on a higher bar (default 4h).

Reference bar (trend_timeframe, e.g. 4h): range_prev = high - low of the last CLOSED 4h bar.
Within the current 4h bar (tracked from 15m bars): target = open_4h + k * range_prev.
Entry (15m): close > target (once per 4h bar), optional 1h-style trend filter via EMA on the 4h
             series (use_trend_filter=1: 4h close > EMA(trend_ema)).
Exit: at the close of the reference bar in which we entered + `hold_bars`-1 more bars
      (time exit, sold at next 15m open), or ATR stop / trail.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.types import Action, Candle, Signal, Timeframe
from app.market.candle_engine import bucket_start
from app.strategy.base import BaseStrategy, StrategyContext


class VolatilityBreakoutStrategy(BaseStrategy):
    name = "volatility_breakout"
    version = "1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._ref_tf: Timeframe = self.trend_timeframe or Timeframe.H4
        self._range_prev: float | None = None
        self._bar_start: datetime | None = None
        self._bar_open: float | None = None
        self._entered_bar: datetime | None = None
        self._bars_held = 0
        self._ref_closes = 0

    def _p(self, k: str, d: float) -> float:
        return float(self.params.get(k, d))

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        if candle.timeframe is self._ref_tf:
            self._range_prev = float(candle.high - candle.low)
            self._ref_closes += 1
            if ctx.position is not None and self._entered_bar is not None:
                self._bars_held += 1
            return None
        if candle.timeframe is not self.timeframe:
            return None
        ind = ctx.indicators.get(self.timeframe, {})
        start = bucket_start(candle.open_time, self._ref_tf)
        if start != self._bar_start:
            self._bar_start, self._bar_open = start, float(candle.open)
        if self._range_prev is None or self._bar_open is None or "atr" not in ind:
            return self._emit(self._hold(candle, "WARMUP", ind))
        k = self._p("k", 0.5)
        target = self._bar_open + k * self._range_prev
        ind = {**ind, "vb_target": target, "vb_range_prev": self._range_prev}
        atr = Decimal(str(ind["atr"]))

        if ctx.position is None:
            if self._entered_bar == start:
                return self._emit(self._hold(candle, "ALREADY_TRADED_THIS_BAR", ind))
            if self._p("use_trend_filter", 1) >= 1:
                t = ctx.indicators.get(self._ref_tf, {})
                if "ema_trend" not in t:
                    return self._emit(self._hold(candle, "TREND_WARMUP", ind))
                if t["close"] <= t["ema_trend"]:
                    return self._emit(self._hold(candle, "TREND_DOWN", ind))
            if float(candle.close) > target:
                self._entered_bar = start
                self._bars_held = 0
                stop = candle.close - atr * Decimal(str(self._p("atr_stop_mult", 2.0)))
                return self._emit(
                    Signal(
                        Action.BUY,
                        1.0,
                        f"VB_BREAKOUT k={k}",
                        candle.close_time,
                        self.name,
                        self.timeframe,
                        ind,
                        stop,
                    )
                )
            return self._emit(self._hold(candle, "BELOW_TARGET", ind))

        pos = ctx.position
        trail = pos.highest_since_entry - atr * Decimal(str(self._p("atr_trail_mult", 3.0)))
        new_stop = max(pos.stop_price or trail, trail)
        if self._bars_held >= int(self._p("hold_bars", 1)):
            return self._emit(
                Signal(
                    Action.SELL,
                    1.0,
                    f"VB_TIME_EXIT after {self._bars_held} bars",
                    candle.close_time,
                    self.name,
                    self.timeframe,
                    ind,
                    new_stop,
                )
            )
        return self._emit(self._hold(candle, "IN_POSITION_TRAIL", ind, new_stop))

    def _hold(
        self, c: Candle, reason: str, ind: dict[str, float], stop: Decimal | None = None
    ) -> Signal:
        return Signal(Action.HOLD, 0.0, reason, c.close_time, self.name, self.timeframe, ind, stop)
