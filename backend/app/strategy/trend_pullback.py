"""Strategy A — Higher-timeframe trend + pullback re-entry.

Trend (1h): close > EMA(trend_ema).                          (ctx.indicators[1h])
Setup (15m): while above EMA(slow), RSI dips below `pullback_rsi` -> armed for `arm_bars` bars.
Trigger: armed and close crosses back above EMA(fast) with RSI > `rsi_exit_low` -> BUY.
Stop: close - ATR*atr_stop_mult ; trail: highest - ATR*atr_trail_mult (ratchet).
Exit: close < EMA(slow) (trend break) -> SELL.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.types import Action, Candle, Signal
from app.strategy.base import BaseStrategy, StrategyContext


class TrendPullbackStrategy(BaseStrategy):
    name = "trend_pullback"
    version = "1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._armed_for = 0
        self._prev_close: float | None = None
        self._prev_fast: float | None = None

    def _p(self, k: str, d: float) -> float:
        return float(self.params.get(k, d))

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        if candle.timeframe is not self.timeframe:
            return None
        ind = ctx.indicators.get(self.timeframe, {})
        if any(k not in ind for k in ("ema_fast", "ema_slow", "rsi", "atr")):
            return self._emit(self._hold(candle, "WARMUP", ind))
        close, fast, slow, rsi = ind["close"], ind["ema_fast"], ind["ema_slow"], ind["rsi"]
        atr = Decimal(str(ind["atr"]))
        prev_close, prev_fast = self._prev_close, self._prev_fast
        self._prev_close, self._prev_fast = close, fast

        if ctx.position is None:
            trend_ok, why = self._trend_ok(ctx)
            if trend_ok and close > slow and rsi < self._p("pullback_rsi", 40):
                self._armed_for = int(self._p("arm_bars", 8))
                return self._emit(self._hold(candle, f"PULLBACK_ARMED RSI={rsi:.1f}", ind))
            if self._armed_for > 0:
                self._armed_for -= 1
                crossed_up = (
                    prev_close is not None
                    and prev_fast is not None
                    and prev_close <= prev_fast
                    and close > fast
                )
                if trend_ok and crossed_up and rsi > self._p("rsi_exit_low", 45):
                    self._armed_for = 0
                    stop = candle.close - atr * Decimal(str(self._p("atr_stop_mult", 2.0)))
                    return self._emit(
                        Signal(
                            Action.BUY,
                            min(1.0, rsi / 100),
                            f"PULLBACK_REENTRY RSI={rsi:.1f}",
                            candle.close_time,
                            self.name,
                            self.timeframe,
                            ind,
                            stop,
                        )
                    )
                return self._emit(
                    self._hold(candle, "ARMED_WAITING_TRIGGER" if trend_ok else why, ind)
                )
            return self._emit(self._hold(candle, why if not trend_ok else "NO_SETUP", ind))

        pos = ctx.position
        trail = pos.highest_since_entry - atr * Decimal(str(self._p("atr_trail_mult", 3.0)))
        new_stop = max(pos.stop_price or trail, trail)
        if close < slow:
            return self._emit(
                Signal(
                    Action.SELL,
                    1.0,
                    "TREND_BREAK_EMA_SLOW",
                    candle.close_time,
                    self.name,
                    self.timeframe,
                    ind,
                    new_stop,
                )
            )
        return self._emit(self._hold(candle, "IN_POSITION_TRAIL", ind, new_stop))

    def _trend_ok(self, ctx: StrategyContext) -> tuple[bool, str]:
        if self.trend_timeframe is None:
            return True, "NO_TREND_FILTER"
        t = ctx.indicators.get(self.trend_timeframe, {})
        if "ema_trend" not in t:
            return False, "TREND_WARMUP"
        return (t["close"] > t["ema_trend"]), (
            "TREND_OK" if t["close"] > t["ema_trend"] else "TREND_DOWN"
        )

    def _hold(
        self, c: Candle, reason: str, ind: dict[str, float], stop: Decimal | None = None
    ) -> Signal:
        return Signal(Action.HOLD, 0.0, reason, c.close_time, self.name, self.timeframe, ind, stop)
