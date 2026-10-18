"""Candle construction.

- `bucket_start(ts, tf)`: floor a UTC timestamp to the bar start of a timeframe.
- `CandleAggregator`: streams closed 1m candles -> emits closed 5m/15m/1h candles (live + backtest).
- `Live1mTracker`: turns WebSocket `candle.1m` updates (same bar repeated) into closed 1m candles.
- `resample_1m`: bulk resample for backtests (pandas), identical bucket semantics.

Missing minutes (no trades) are tolerated: a bucket closes when the first 1m candle of a LATER
bucket arrives. We never invent bars for empty buckets.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.core.types import Candle, Timeframe

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(ts: datetime, tf: Timeframe) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be tz-aware")
    ts = ts.astimezone(UTC)
    secs = int((ts - _EPOCH).total_seconds())
    size = tf.minutes * 60
    return _EPOCH + timedelta(seconds=secs - secs % size)


class _Bucket:
    def __init__(self, first: Candle, tf: Timeframe) -> None:
        self.tf = tf
        self.open_time = bucket_start(first.open_time, tf)
        self.market = first.market
        self.open = first.open
        self.high = first.high
        self.low = first.low
        self.close = first.close
        self.volume = first.volume
        self.quote_volume = first.quote_volume
        self.count = 1

    def add(self, c: Candle) -> None:
        self.high = max(self.high, c.high)
        self.low = min(self.low, c.low)
        self.close = c.close
        self.volume += c.volume
        self.quote_volume += c.quote_volume
        self.count += 1

    def to_candle(self) -> Candle:
        return Candle(
            market=self.market,
            timeframe=self.tf,
            open_time=self.open_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            quote_volume=self.quote_volume,
            is_closed=True,
        )


class CandleAggregator:
    """Feed closed base candles (default 1m) in order; get back closed higher-timeframe candles.

    Base may be any timeframe that divides the targets (e.g. 15m base -> 1h) — used to run
    fast walk-forward sweeps on pre-resampled data with identical bucket semantics.
    """

    def __init__(self, timeframes: Iterable[Timeframe], base: Timeframe = Timeframe.M1) -> None:
        self._base = base
        self._tfs = [tf for tf in timeframes if tf is not base]
        for tf in self._tfs:
            if tf.minutes % base.minutes != 0 or tf.minutes < base.minutes:
                raise ValueError(f"{tf} is not a multiple of base {base}")
        self._open: dict[Timeframe, _Bucket] = {}
        self._last: datetime | None = None

    def add(self, c: Candle) -> list[Candle]:
        if c.timeframe is not self._base:
            raise ValueError(f"CandleAggregator expects {self._base} candles, got {c.timeframe}")
        if self._last is not None and c.open_time <= self._last:
            raise ValueError(f"out-of-order candle {c.open_time} <= {self._last}")
        self._last = c.open_time
        closed: list[Candle] = []
        for tf in self._tfs:
            b = self._open.get(tf)
            start = bucket_start(c.open_time, tf)
            if b is None:
                self._open[tf] = _Bucket(c, tf)
            elif start > b.open_time:
                closed.append(b.to_candle())
                self._open[tf] = _Bucket(c, tf)
            else:
                b.add(c)
        return closed

    def add_1m(self, c: Candle) -> list[Candle]:
        return self.add(c)

    def flush(self) -> list[Candle]:
        """Close all in-progress buckets (end of backtest). Not used live."""
        out = [b.to_candle() for b in self._open.values()]
        self._open.clear()
        return out


class Live1mTracker:
    """WS candle.1m repeats the CURRENT bar; a bar is closed when a newer bar appears."""

    def __init__(self) -> None:
        self.current: Candle | None = None

    def update(self, c: Candle) -> Candle | None:
        if self.current is None or c.open_time == self.current.open_time:
            self.current = replace(c, is_closed=False)
            return None
        if c.open_time < self.current.open_time:
            return None  # stale duplicate, ignore
        closed = replace(self.current, is_closed=True)
        self.current = replace(c, is_closed=False)
        return closed


def candles_to_frame(candles: Iterable[Candle]) -> pd.DataFrame:
    rows = [
        {
            "open_time": c.open_time,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
            "quote_volume": float(c.quote_volume),
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return (
        df.set_index(pd.DatetimeIndex(df["open_time"], tz="UTC"))
        .drop(columns=["open_time"])
        .sort_index()
    )


def resample_1m(candles: list[Candle], tf: Timeframe) -> list[Candle]:
    """Bulk resample via the aggregator so semantics match live exactly."""
    base = candles[0].timeframe if candles else Timeframe.M1
    agg = CandleAggregator([tf], base=base)
    out: list[Candle] = []
    for c in candles:
        out.extend(agg.add(c))
    out.extend(agg.flush())
    return out


def frame_to_candles(df: pd.DataFrame, market: str, tf: Timeframe) -> list[Candle]:
    out: list[Candle] = []
    for ts, row in df.iterrows():
        assert isinstance(ts, pd.Timestamp)
        out.append(
            Candle(
                market=market,
                timeframe=tf,
                open_time=ts.to_pydatetime().astimezone(UTC),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                quote_volume=Decimal(str(row["quote_volume"])),
            )
        )
    return out
