"""Strategies D / E — low-frequency trend following (breakout entry, ATR stop, trend exit).

D `trend_following_4h`: strategy TF 4h, trend TF 1d.
E `trend_following_1d`: strategy TF 1d, trend TF 1d (EMA on the same daily series).

Trend filter : trend-TF close > EMA(trend_ema)
Entry        : close > highest high of the previous `entry_n` bars
Initial stop : close - ATR * atr_stop_mult ; trailing: highest - ATR * atr_trail_mult (ratchet only)
Exit         : close < lowest low of the previous `exit_n` bars (channel exit)
               or, when exit_on_trend_loss=1, trend-TF close < EMA(trend_ema)
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from app.core.types import Action, Candle, Signal
from app.strategy.base import BaseStrategy, StrategyContext


class TrendFollowingBreakout(BaseStrategy):
    name = "trend_following"
    version = "1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        n = int(float(self.params.get("entry_n", 20)))
        m = int(float(self.params.get("exit_n", 10)))
        self._highs: deque[float] = deque(maxlen=n)
        self._lows: deque[float] = deque(maxlen=m)

    def _p(self, k: str, d: float) -> float:
        return float(self.params.get(k, d))

    def _trend(self, ctx: StrategyContext) -> tuple[bool | None, float | None, float | None]:
        tf = self.trend_timeframe or self.timeframe
        t = ctx.indicators.get(tf, {})
        if "ema_trend" not in t:
            return None, None, None
        return t["close"] > t["ema_trend"], t["ema_trend"], t["close"]

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        if candle.timeframe is not self.timeframe:
            return None
        ind = ctx.indicators.get(self.timeframe, {})
        upper = max(self._highs) if len(self._highs) == self._highs.maxlen else None
        lower = min(self._lows) if len(self._lows) == self._lows.maxlen else None
        self._highs.append(float(candle.high))
        self._lows.append(float(candle.low))
        trend_up, ema_t, close_t = self._trend(ctx)
        if "atr" not in ind or upper is None or lower is None or trend_up is None:
            return self._emit(self._hold(candle, "WARMUP", ind))
        atr = Decimal(str(ind["atr"]))
        close = float(candle.close)
        ind = {
            **ind,
            "channel_upper": upper,
            "channel_lower": lower,
            "trend_ema": float(ema_t or 0),
            "close_trend": float(close_t or 0),
        }

        if ctx.position is None:
            if not trend_up:
                return self._emit(self._hold(candle, "TREND_DOWN", ind))
            if close > upper:
                stop = candle.close - atr * Decimal(str(self._p("atr_stop_mult", 3.0)))
                return self._emit(
                    Signal(
                        Action.BUY,
                        1.0,
                        f"BREAKOUT>{upper:.0f} TREND_UP",
                        candle.close_time,
                        self.name,
                        self.timeframe,
                        ind,
                        stop,
                    )
                )
            return self._emit(self._hold(candle, "INSIDE_CHANNEL", ind))

        pos = ctx.position
        trail = pos.highest_since_entry - atr * Decimal(str(self._p("atr_trail_mult", 4.0)))
        new_stop = max(pos.stop_price or trail, trail)
        if close < lower:
            return self._emit(
                Signal(
                    Action.SELL,
                    1.0,
                    f"CHANNEL_EXIT<{lower:.0f}",
                    candle.close_time,
                    self.name,
                    self.timeframe,
                    ind,
                    new_stop,
                )
            )
        if self._p("exit_on_trend_loss", 1) >= 1 and not trend_up:
            return self._emit(
                Signal(
                    Action.SELL,
                    1.0,
                    "TREND_LOSS_EXIT",
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


class TrendFollowing4h(TrendFollowingBreakout):
    name = "trend_following_4h"


class TrendFollowing1d(TrendFollowingBreakout):
    name = "trend_following_1d"
