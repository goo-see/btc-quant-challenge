"""Client-side rate limiting per Upbit group + Remaining-Req header feedback.

Official limits (2026-09-24): quotation groups 10/s (IP); exchange default 30/s,
order 12/s, order-test 8/s, order-cancel-all 1 per 2s (per pocket); ws-connect 5/s.
We run at ~50-60% of each limit and slow further when `Remaining-Req: ...; sec=N` gets low.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

_REMAINING_RE = re.compile(r"group=(?P<group>[\w-]+);\s*min=(?P<min>\d+);\s*sec=(?P<sec>\d+)")

# group -> our permitted requests per second (below official)
DEFAULT_GROUP_RATES: dict[str, float] = {
    "market": 5.0,
    # live 2026-09-24: 6 req/s sequential fetch still produced ~10% HTTP 429 -> run slower
    "candle": 4.0,
    "trade": 5.0,
    "ticker": 5.0,
    "orderbook": 5.0,
    "default": 15.0,
    "order": 6.0,
    "order-test": 4.0,
    "order-cancel-all": 0.4,
    "websocket-connect": 2.0,
}


@dataclass(slots=True)
class RemainingReq:
    group: str
    minute: int
    second: int


# Server header group names observed live (2026-09-24: "group=candles; min=600; sec=9")
# do not always match the documented group names we throttle on.
_GROUP_ALIASES: dict[str, str] = {"candles": "candle", "trades": "trade", "tickers": "ticker"}


def parse_remaining_req(header: str | None) -> RemainingReq | None:
    if not header:
        return None
    m = _REMAINING_RE.search(header)
    if not m:
        return None
    group = m.group("group")
    return RemainingReq(_GROUP_ALIASES.get(group, group), int(m.group("min")), int(m.group("sec")))


@dataclass(slots=True)
class _Bucket:
    rate: float
    capacity: float
    tokens: float
    updated: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    penalty_until: float = 0.0


class RateLimiter:
    def __init__(self, rates: dict[str, float] | None = None, monotonic: object = None) -> None:
        self._rates = dict(DEFAULT_GROUP_RATES)
        if rates:
            self._rates.update(rates)
        self._buckets: dict[str, _Bucket] = {}
        self._clock = monotonic or time.monotonic
        self.waits = 0
        self.penalties = 0

    def _bucket(self, group: str) -> _Bucket:
        b = self._buckets.get(group)
        if b is None:
            rate = self._rates.get(group, 5.0)
            b = _Bucket(
                rate=rate, capacity=max(1.0, rate), tokens=max(1.0, rate), updated=self._now()
            )
            self._buckets[group] = b
        return b

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    async def acquire(self, group: str) -> None:
        b = self._bucket(group)
        async with b.lock:
            while True:
                now = self._now()
                if now < b.penalty_until:
                    self.waits += 1
                    await asyncio.sleep(b.penalty_until - now)
                    continue
                elapsed = now - b.updated
                b.tokens = min(b.capacity, b.tokens + elapsed * b.rate)
                b.updated = now
                if b.tokens >= 1.0:
                    b.tokens -= 1.0
                    return
                self.waits += 1
                await asyncio.sleep((1.0 - b.tokens) / b.rate)

    def feedback(self, header: str | None, status_code: int | None = None) -> None:
        """Adapt to server feedback. sec<=1 -> pause 1s; 429 -> 1s; 418 -> 60s."""
        b: _Bucket | None = None
        rem = parse_remaining_req(header)
        if rem is not None:
            b = self._bucket(rem.group)
            if rem.second <= 1:
                b.penalty_until = max(b.penalty_until, self._now() + 1.0)
                self.penalties += 1
        if status_code == 429 and b is not None:
            b.penalty_until = max(b.penalty_until, self._now() + 1.0)
            self.penalties += 1
        if status_code == 418:
            self.penalties += 1
            for bucket in self._buckets.values():
                bucket.penalty_until = max(bucket.penalty_until, self._now() + 60.0)
