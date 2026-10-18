"""Incremental indicators (EMA, RSI-Wilder, ATR-Wilder, SMA). Same code path in backtest and live.

Floats are acceptable here: indicators feed decisions, never accounting.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.core.types import Candle


class EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period >= 1")
        self.period = period
        self._k = 2.0 / (period + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period  # SMA seed
            return self.value
        self.value = (x - self.value) * self._k + self.value
        return self.value


class SMA:
    def __init__(self, period: int) -> None:
        self.period = period
        self._win: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if len(self._win) == self.period:
            self._sum -= self._win[0]
        self._win.append(x)
        self._sum += x
        if len(self._win) == self.period:
            self.value = self._sum / self.period
        return self.value


class RSI:
    """Wilder's RSI: first average = simple mean of `period` changes, then smoothed."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None or self._avg_loss is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's ATR on true range."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._trs: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = (
            high - low
            if self._prev_close is None
            else max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        )
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self.value = sum(self._trs) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


@dataclass
class IndicatorSet:
    """Bundle updated once per closed candle of one timeframe."""

    ema_fast: EMA
    ema_slow: EMA
    ema_trend: EMA
    rsi: RSI
    atr: ATR
    vol_sma: SMA
    bars: int = 0
    last: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_params(cls, p: dict[str, float]) -> IndicatorSet:
        return cls(
            ema_fast=EMA(int(p.get("ema_fast", 12))),
            ema_slow=EMA(int(p.get("ema_slow", 48))),
            ema_trend=EMA(int(p.get("trend_ema", 200))),
            rsi=RSI(int(p.get("rsi_period", 14))),
            atr=ATR(int(p.get("atr_period", 14))),
            vol_sma=SMA(int(p.get("volume_sma_period", 20))),
        )

    def update(self, c: Candle) -> dict[str, float]:
        close, high, low, vol = float(c.close), float(c.high), float(c.low), float(c.volume)
        self.bars += 1
        out: dict[str, float] = {"close": close, "volume": vol}
        prev_fast, prev_slow = self.ema_fast.value, self.ema_slow.value
        if (v := self.ema_fast.update(close)) is not None:
            out["ema_fast"] = v
        if (v := self.ema_slow.update(close)) is not None:
            out["ema_slow"] = v
        if (v := self.ema_trend.update(close)) is not None:
            out["ema_trend"] = v
        if (v := self.rsi.update(close)) is not None:
            out["rsi"] = v
        if (v := self.atr.update(high, low, close)) is not None:
            out["atr"] = v
        if (v := self.vol_sma.update(vol)) is not None:
            out["vol_sma"] = v
        if (
            prev_fast is not None
            and prev_slow is not None
            and "ema_fast" in out
            and "ema_slow" in out
        ):
            out["prev_ema_fast"], out["prev_ema_slow"] = prev_fast, prev_slow
        self.last = out
        return out

    @property
    def ready(self) -> bool:
        return all(k in self.last for k in ("ema_fast", "ema_slow", "rsi", "atr", "vol_sma"))
