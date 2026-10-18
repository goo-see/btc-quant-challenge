"""Strategy B — Donchian channel breakout + ATR stop.

Entry (15m): close > highest high of the previous `entry_n` bars (channel breakout),
             optional 1h trend filter (close > EMA(trend_ema)) when use_trend_filter=1.
Stop: close - ATR*atr_stop_mult ; trail: highest - ATR*atr_trail_mult (ratchet).
Exit: close < lowest low of the previous `exit_n` bars -> SELL.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from app.core.types import Action, Candle, Signal
from app.strategy.base import BaseStrategy, StrategyContext


class DonchianBreakoutStrategy(BaseStrategy):
    name = "donchian_breakout"
    version = "1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        n = int(float(self.params.get("entry_n", 40)))
        m = int(float(self.params.get("exit_n", 20)))
        self._highs: deque[float] = deque(maxlen=n)
        self._lows: deque[float] = deque(maxlen=m)

    def _p(self, k: str, d: float) -> float:
        return float(self.params.get(k, d))

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        if candle.timeframe is not self.timeframe:
            return None
        ind = ctx.indicators.get(self.timeframe, {})
        high, low, close = float(candle.high), float(candle.low), float(candle.close)
        upper = max(self._highs) if len(self._highs) == self._highs.maxlen else None
        lower = min(self._lows) if len(self._lows) == self._lows.maxlen else None
        self._highs.append(high)
        self._lows.append(low)
        if "atr" not in ind or upper is None or lower is None:
            return self._emit(self._hold(candle, "WARMUP", ind))
        atr = Decimal(str(ind["atr"]))
        ind = {**ind, "donchian_upper": upper, "donchian_lower": lower}

        if ctx.position is None:
            if self._p("use_trend_filter", 1) >= 1:
                t = ctx.indicators.get(self.trend_timeframe, {}) if self.trend_timeframe else {}
                if "ema_trend" not in t:
                    return self._emit(self._hold(candle, "TREND_WARMUP", ind))
                if t["close"] <= t["ema_trend"]:
                    return self._emit(self._hold(candle, "TREND_DOWN", ind))
            if close > upper:
                stop = candle.close - atr * Decimal(str(self._p("atr_stop_mult", 3.0)))
                return self._emit(
                    Signal(
                        Action.BUY,
                        1.0,
                        f"DONCHIAN_BREAKOUT>{upper:.0f}",
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
                    f"DONCHIAN_EXIT<{lower:.0f}",
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
