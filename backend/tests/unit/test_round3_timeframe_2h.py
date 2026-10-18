"""Round 3: the 2h Timeframe member and 1m/1h -> 2h aggregation (D0 path unchanged)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.types import Candle, Timeframe
from app.market.candle_engine import CandleAggregator, bucket_start, resample_1m


def _c(tf: Timeframe, t: datetime, px: float = 100.0) -> Candle:
    d = Decimal(str(px))
    return Candle("KRW-BTC", tf, t, d, d + 1, d - 1, d, Decimal(1), Decimal(100))


def test_2h_member_and_minutes() -> None:
    assert Timeframe("2h") is Timeframe.H2
    assert Timeframe.H2.minutes == 120
    assert Timeframe.H2.delta == timedelta(hours=2)
    # the existing members (used by D0) are untouched
    assert [t.value for t in Timeframe] == ["1m", "5m", "15m", "1h", "2h", "4h", "1d"]
    assert Timeframe.H4.minutes == 240 and Timeframe.D1.minutes == 1440


def test_2h_buckets_are_even_utc_hours() -> None:
    t = datetime(2026, 9, 26, 15, 37, tzinfo=UTC)
    assert bucket_start(t, Timeframe.H2) == datetime(2026, 9, 26, 14, 0, tzinfo=UTC)
    assert bucket_start(t, Timeframe.H4) == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert bucket_start(datetime(2026, 9, 26, 1, 59, tzinfo=UTC), Timeframe.H2) == datetime(
        2026, 9, 26, 0, 0, tzinfo=UTC
    )


def test_1h_to_2h_aggregation_closes_on_next_bucket() -> None:
    agg = CandleAggregator([Timeframe.H2, Timeframe.H4], base=Timeframe.H1)
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    out: list[Candle] = []
    for i in range(5):  # 00,01,02,03,04 -> 2h bars [00-02) and [02-04) close, [04-06) open
        out.extend(agg.add(_c(Timeframe.H1, t0 + timedelta(hours=i), 100 + i)))
    h2 = [c for c in out if c.timeframe is Timeframe.H2]
    assert [c.open_time.hour for c in h2] == [0, 2]
    assert h2[0].open == Decimal(100) and h2[0].close == Decimal(101)
    assert h2[0].high == Decimal(102) and h2[0].low == Decimal(99)
    assert h2[0].close_time == datetime(2026, 9, 26, 2, 0, tzinfo=UTC)
    h4 = [c for c in out if c.timeframe is Timeframe.H4]  # the 04:00 bar closes [00-04)
    assert [c.open_time.hour for c in h4] == [0] and h4[0].close == Decimal(103)


def test_1m_and_1h_bases_give_identical_2h_bars() -> None:
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    m1 = [_c(Timeframe.M1, t0 + timedelta(minutes=i), 100 + (i * 7) % 13) for i in range(6 * 60)]
    h1 = resample_1m(m1, Timeframe.H1)
    via_1m = resample_1m(m1, Timeframe.H2)
    via_1h = resample_1m(h1, Timeframe.H2)
    assert len(via_1m) == len(via_1h) == 3
    for a, b in zip(via_1m, via_1h, strict=True):
        assert (a.open_time, a.open, a.high, a.low, a.close, a.volume) == (
            b.open_time,
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
        )
