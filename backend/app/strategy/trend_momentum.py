"""Baseline: EMA trend + RSI momentum + volume filter + ATR stop / trailing stop.

All thresholds come from config `strategy.params`; nothing is hard-coded as "correct".

Entry (own TF, no position):
  - higher-TF close > higher-TF EMA(trend_ema)            (trend filter)
  - EMA(fast) crossed above EMA(slow) within `cross_lookback` bars
  - rsi_buy_min <= RSI <= rsi_buy_max
  - volume >= vol_sma * volume_min_ratio
  -> BUY, stop = close - atr * atr_stop_mult
In position:
  - trailing stop = highest_since_entry - atr * atr_trail_mult (only ratchets up)
  - EMA(fast) < EMA(slow) -> SELL (trend exit)
  - otherwise HOLD carrying the updated stop_price (engine enforces it intrabar)
"""

from __future__ import annotations

from decimal import Decimal

from app.core.types import Action, Candle, Signal
from app.strategy.base import BaseStrategy, StrategyContext


class TrendMomentumStrategy(BaseStrategy):
    name = "trend_momentum"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._bars_since_cross: int | None = None

    def _p(self, key: str, default: float) -> float:
        return float(self.params.get(key, default))

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> Signal | None:
        if candle.timeframe is not self.timeframe:
            return None
        ind = ctx.indicators.get(self.timeframe, {})
        needed = ("ema_fast", "ema_slow", "rsi", "atr", "vol_sma", "prev_ema_fast", "prev_ema_slow")
        if any(k not in ind for k in needed):
            return self._emit(self._hold(candle, "WARMUP", ind))

        fast, slow = ind["ema_fast"], ind["ema_slow"]
        if ind["prev_ema_fast"] <= ind["prev_ema_slow"] and fast > slow:
            self._bars_since_cross = 0
        elif self._bars_since_cross is not None:
            self._bars_since_cross += 1

        atr = Decimal(str(ind["atr"]))
        close = candle.close

        if ctx.position is None:
            trend_ok, trend_reason = self._trend_ok(ctx, close)
            if not trend_ok:
                return self._emit(self._hold(candle, trend_reason, ind))
            lookback = int(self._p("cross_lookback", 3))
            if self._bars_since_cross is None or self._bars_since_cross > lookback:
                return self._emit(self._hold(candle, "NO_RECENT_GOLDEN_CROSS", ind))
            rsi = ind["rsi"]
            lo, hi = self._p("rsi_buy_min", 50), self._p("rsi_buy_max", 70)
            if not (lo <= rsi <= hi):
                return self._emit(self._hold(candle, f"RSI_OUT_OF_RANGE({rsi:.1f})", ind))
            if ind["volume"] < ind["vol_sma"] * self._p("volume_min_ratio", 1.0):
                return self._emit(self._hold(candle, "VOLUME_BELOW_SMA", ind))
            stop = close - atr * Decimal(str(self._p("atr_stop_mult", 2.0)))
            strength = min(1.0, max(0.0, (rsi - lo) / max(hi - lo, 1e-9)))
            return self._emit(
                Signal(
                    action=Action.BUY,
                    strength=strength,
                    reason=f"GOLDEN_CROSS+{self._bars_since_cross}b TREND_OK RSI={rsi:.1f} VOL_OK",
                    timestamp=candle.close_time,
                    strategy=self.name,
                    timeframe=self.timeframe,
                    indicators=ind,
                    stop_price=stop,
                )
            )

        # in position
        pos = ctx.position
        trail = pos.highest_since_entry - atr * Decimal(str(self._p("atr_trail_mult", 3.0)))
        new_stop = max(pos.stop_price or trail, trail)
        if fast < slow:
            return self._emit(
                Signal(
                    action=Action.SELL,
                    strength=1.0,
                    reason="EMA_DEATH_CROSS",
                    timestamp=candle.close_time,
                    strategy=self.name,
                    timeframe=self.timeframe,
                    indicators=ind,
                    stop_price=new_stop,
                )
            )
        return self._emit(self._hold(candle, "IN_POSITION_TRAIL", ind, new_stop))

    def _trend_ok(self, ctx: StrategyContext, close: Decimal) -> tuple[bool, str]:
        if self.trend_timeframe is None:
            return True, "NO_TREND_FILTER"
        t = ctx.indicators.get(self.trend_timeframe, {})
        if "ema_trend" not in t or "close" not in t:
            return False, "TREND_WARMUP"
        if t["close"] <= t["ema_trend"]:
            return False, "TREND_DOWN"
        return True, "TREND_OK"

    def _hold(
        self, candle: Candle, reason: str, ind: dict[str, float], stop: Decimal | None = None
    ) -> Signal:
        return Signal(
            action=Action.HOLD,
            strength=0.0,
            reason=reason,
            timestamp=candle.close_time,
            strategy=self.name,
            timeframe=self.timeframe,
            indicators=ind,
            stop_price=stop,
        )
