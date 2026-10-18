"""Clock abstraction so strategy/risk code is identical in backtest and live."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class BacktestClock:
    """Manually advanced clock. `sleep` returns immediately."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("BacktestClock requires tz-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        if ts < self._now:
            raise ValueError(f"clock cannot move backwards: {ts} < {self._now}")
        self._now = ts

    async def sleep(self, seconds: float) -> None:
        return None
