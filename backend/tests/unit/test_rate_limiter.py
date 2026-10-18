from __future__ import annotations

import asyncio

from app.exchange.upbit.rate_limiter import RateLimiter, parse_remaining_req


def test_parse_remaining_req() -> None:
    r = parse_remaining_req("group=default; min=1800; sec=29")
    assert r is not None and (r.group, r.minute, r.second) == ("default", 1800, 29)
    assert parse_remaining_req(None) is None
    assert parse_remaining_req("garbage") is None


def test_parse_remaining_req_aliases_live_group_name() -> None:
    r = parse_remaining_req("group=candles; min=600; sec=9")
    assert r is not None and r.group == "candle"


async def test_bucket_waits_when_exhausted() -> None:
    rl = RateLimiter({"order": 2.0})
    t0 = asyncio.get_running_loop().time()
    for _ in range(4):  # capacity 2 -> two immediate, two must wait ~0.5s each
        await rl.acquire("order")
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed >= 0.9
    assert rl.waits >= 2


async def test_penalty_on_low_remaining_and_418() -> None:
    rl = RateLimiter({"candle": 100.0})
    rl.feedback("group=candles; min=600; sec=0", 200)
    assert rl.penalties == 1
    t0 = asyncio.get_running_loop().time()
    await rl.acquire("candle")
    assert asyncio.get_running_loop().time() - t0 >= 0.9
    rl.feedback(None, 418)
    assert rl.penalties == 2
