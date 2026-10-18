from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.types import Candle, Timeframe

BASE_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def make_candle(
    i: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1.0,
    tf: Timeframe = Timeframe.M1,
    start: datetime = BASE_TIME,
) -> Candle:
    return Candle(
        market="KRW-BTC",
        timeframe=tf,
        open_time=start + tf.delta * i,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        quote_volume=Decimal(str(volume * close)),
    )


def synthetic_1m(
    n: int,
    *,
    seed: int = 7,
    start_price: float = 100_000_000.0,
    drift: float = 0.0,
    start: datetime = BASE_TIME,
) -> list[Candle]:
    """Random-walk 1m candles with valid OHLC."""
    rng = random.Random(seed)
    out: list[Candle] = []
    price = start_price
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(drift, 0.0008))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
        out.append(
            make_candle(
                i,
                open_=round(o),
                high=round(h),
                low=round(lo),
                close=round(c),
                volume=rng.uniform(0.1, 2.0),
                start=start,
            )
        )
        price = c
    return out


@pytest.fixture
def candles_1m() -> list[Candle]:
    return synthetic_1m(3000)


def trend_1m(
    n: int,
    *,
    up_bars: int,
    down_bars: int,
    start_price: float = 100_000_000.0,
    up_step: float = 0.0006,
    down_step: float = -0.0005,
) -> list[Candle]:
    """Deterministic saw-tooth: `up_bars` rising then `down_bars` falling, repeated."""
    out: list[Candle] = []
    price = start_price
    i = 0
    while i < n:
        for step in [up_step] * up_bars + [down_step] * down_bars:
            if i >= n:
                break
            o = price
            c = o * (1 + step)
            out.append(
                make_candle(
                    i,
                    open_=round(o),
                    high=round(max(o, c) * 1.0002),
                    low=round(min(o, c) * 0.9998),
                    close=round(c),
                    volume=1.0 + (i % 5) * 0.3,
                )
            )
            price = c
            i += 1
    return out


__all__ = ["BASE_TIME", "make_candle", "synthetic_1m", "timedelta", "trend_1m"]
