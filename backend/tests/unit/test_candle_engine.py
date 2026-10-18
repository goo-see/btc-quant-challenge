from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.types import Timeframe
from app.market.candle_engine import CandleAggregator, Live1mTracker, bucket_start, resample_1m
from tests.conftest import BASE_TIME, make_candle, synthetic_1m


def test_bucket_start() -> None:
    ts = datetime(2026, 3, 5, 13, 47, 12, tzinfo=UTC)
    assert bucket_start(ts, Timeframe.M1) == datetime(2026, 3, 5, 13, 47, tzinfo=UTC)
    assert bucket_start(ts, Timeframe.M5) == datetime(2026, 3, 5, 13, 45, tzinfo=UTC)
    assert bucket_start(ts, Timeframe.M15) == datetime(2026, 3, 5, 13, 45, tzinfo=UTC)
    assert bucket_start(ts, Timeframe.H1) == datetime(2026, 3, 5, 13, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="tz-aware"):
        bucket_start(datetime(2026, 1, 1), Timeframe.M1)


def test_aggregator_builds_5m_from_1m_with_correct_ohlcv() -> None:
    c = [
        make_candle(0, open_=100, high=105, low=99, close=101, volume=1),
        make_candle(1, open_=101, high=110, low=100, close=108, volume=2),
        make_candle(2, open_=108, high=109, low=95, close=96, volume=3),
        make_candle(3, open_=96, high=97, low=94, close=95, volume=4),
        make_candle(4, open_=95, high=98, low=93, close=97, volume=5),
        make_candle(
            5, open_=97, high=99, low=96, close=98, volume=1
        ),  # next bucket -> closes first
    ]
    agg = CandleAggregator([Timeframe.M5])
    closed = []
    for x in c:
        closed.extend(agg.add(x))
    assert len(closed) == 1
    b = closed[0]
    assert b.open_time == BASE_TIME
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal(100),
        Decimal(110),
        Decimal(93),
        Decimal(97),
        Decimal(15),
    )
    assert agg.flush()[0].open_time == BASE_TIME + timedelta(minutes=5)


def test_aggregator_tolerates_missing_minutes() -> None:
    c = [
        make_candle(0, open_=1, high=1, low=1, close=1),
        make_candle(3, open_=2, high=2, low=2, close=2),
        make_candle(7, open_=3, high=3, low=3, close=3),
    ]
    agg = CandleAggregator([Timeframe.M5])
    closed = []
    for x in c:
        closed.extend(agg.add(x))
    assert len(closed) == 1 and closed[0].close == Decimal(2)


def test_aggregator_rejects_out_of_order() -> None:
    agg = CandleAggregator([Timeframe.M5])
    agg.add(make_candle(1, open_=1, high=1, low=1, close=1))
    with pytest.raises(ValueError, match="out-of-order"):
        agg.add(make_candle(0, open_=1, high=1, low=1, close=1))


def test_resample_matches_pandas_reference() -> None:
    candles = synthetic_1m(1000)
    ours = resample_1m(candles, Timeframe.M15)
    import pandas as pd

    from app.market.candle_engine import candles_to_frame

    df = candles_to_frame(candles)
    ref = df.resample("15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    assert len(ours) == len(ref)
    for c, (ts, row) in zip(ours, ref.iterrows(), strict=True):
        assert isinstance(ts, pd.Timestamp)
        assert c.open_time == ts.to_pydatetime()
        assert float(c.open) == row["open"] and float(c.high) == row["high"]
        assert float(c.low) == row["low"] and float(c.close) == row["close"]
        assert abs(float(c.volume) - row["volume"]) < 1e-9


def test_resample_15m_to_1h_matches_direct() -> None:
    candles = synthetic_1m(24 * 60 * 3)
    direct = resample_1m(candles, Timeframe.H1)
    via15 = resample_1m(resample_1m(candles, Timeframe.M15), Timeframe.H1)
    assert [(c.open_time, c.open, c.high, c.low, c.close) for c in direct] == [
        (c.open_time, c.open, c.high, c.low, c.close) for c in via15
    ]


def test_live_tracker_closes_on_new_bar_and_ignores_stale() -> None:
    t = Live1mTracker()
    a1 = make_candle(0, open_=1, high=2, low=1, close=1.5)
    a2 = make_candle(0, open_=1, high=3, low=1, close=2.5)  # same bar updated
    b = make_candle(1, open_=2.5, high=3, low=2, close=2)
    assert t.update(a1) is None
    assert t.update(a2) is None
    closed = t.update(b)
    assert closed is not None and closed.is_closed and closed.high == Decimal(3)
    assert t.update(a1) is None  # stale
    assert t.current is not None and t.current.open_time == b.open_time
