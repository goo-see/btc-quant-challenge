"""Historical candle sync from Upbit REST (raw rows per timeframe in `candles`; 1m is the source of
truth for the recent 2y, 1h (unit=60) extends coverage back to the 2017-09 listing).

Resumable: the target window is compared with what the DB already holds, and only the
missing head (older) and tail (newer) are fetched. Re-running is idempotent (insert-or-ignore).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.core.types import Candle, Timeframe
from app.database.repository import Repository
from app.exchange.upbit.models import UpbitCandle
from app.exchange.upbit.rest_client import CANDLE_MAX_COUNT, UpbitRestClient

log = get_logger("historical")


def to_candle(u: UpbitCandle, tf: Timeframe) -> Candle:
    return Candle(
        market=u.market,
        timeframe=tf,
        open_time=u.open_time,
        open=u.opening_price,
        high=u.high_price,
        low=u.low_price,
        close=u.trade_price,
        volume=u.candle_acc_trade_volume,
        quote_volume=u.candle_acc_trade_price,
    )


@dataclass(slots=True)
class SyncReport:
    market: str
    requested_start: datetime
    requested_end: datetime
    requests: int = 0
    fetched: int = 0
    db_min: datetime | None = None
    db_max: datetime | None = None
    db_count: int = 0


@dataclass(slots=True)
class ValidationReport:
    count: int
    first: datetime | None
    last: datetime | None
    expected_minutes: int
    missing_minutes: int
    duplicates: int
    out_of_order: int
    gaps: list[tuple[datetime, datetime, int]] = field(default_factory=list)  # (from, to, minutes)
    ohlc_violations: int = 0

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.count / self.expected_minutes if self.expected_minutes else 0.0


class HistoricalSync:
    def __init__(
        self, client: UpbitRestClient, repo: Repository, *, progress_every: int = 25
    ) -> None:
        self._client = client
        self._repo = repo
        self._progress_every = progress_every

    async def _fetch_backward(
        self,
        market: str,
        to: datetime,
        until: datetime,
        report: SyncReport,
        tf: Timeframe = Timeframe.M1,
    ) -> None:
        """Fetch `tf` candles ending at `to` going backwards until open_time < until."""
        cursor = to
        while cursor > until:
            batch = await self._client.get_minute_candles(
                market, tf.upbit_unit, count=CANDLE_MAX_COUNT, to=cursor
            )
            report.requests += 1
            if not batch:
                log.warning("history_empty_batch", to=cursor.isoformat())
                break
            candles = [to_candle(u, tf) for u in batch if u.open_time >= until]
            if candles:
                await self._repo.upsert_candles(candles, source="rest")
                report.fetched += len(candles)
            oldest = min(u.open_time for u in batch)
            if oldest >= cursor:  # no progress -> avoid infinite loop
                log.warning("history_no_progress", cursor=cursor.isoformat())
                break
            cursor = oldest
            if report.requests % self._progress_every == 0:
                log.info(
                    "history_progress",
                    requests=report.requests,
                    fetched=report.fetched,
                    cursor=cursor.isoformat(),
                )

    async def sync(
        self,
        market: str,
        start: datetime,
        end: datetime | None = None,
        tf: Timeframe = Timeframe.M1,
    ) -> SyncReport:
        end = end or datetime.now(UTC).replace(second=0, microsecond=0)
        report = SyncReport(market=market, requested_start=start, requested_end=end)
        db_min, db_max, _ = await self._repo.candle_range(market, tf)
        step = tf.delta

        if db_min is None or db_max is None:
            await self._fetch_backward(market, end, start, report, tf)
        else:
            if db_min > start:  # older head missing
                await self._fetch_backward(market, db_min, start, report, tf)
            if db_max + step < end:  # newer tail missing
                await self._fetch_backward(market, end, db_max + step, report, tf)

        report.db_min, report.db_max, report.db_count = await self._repo.candle_range(market, tf)
        log.info(
            "history_sync_done",
            requests=report.requests,
            fetched=report.fetched,
            db_count=report.db_count,
            db_min=report.db_min.isoformat() if report.db_min else None,
            db_max=report.db_max.isoformat() if report.db_max else None,
        )
        return report


def validate_1m(candles: list[Candle], *, max_gaps_listed: int = 50) -> ValidationReport:
    return validate_series(candles, max_gaps_listed=max_gaps_listed)


def validate_series(candles: list[Candle], *, max_gaps_listed: int = 50) -> ValidationReport:
    """Duplicates / ordering / gaps / OHLC sanity for any timeframe (gap unit = that timeframe)."""
    if not candles:
        return ValidationReport(0, None, None, 0, 0, 0, 0)
    step_min = candles[0].timeframe.minutes
    seen: set[datetime] = set()
    dups = 0
    ooo = 0
    ohlc_bad = 0
    gaps: list[tuple[datetime, datetime, int]] = []
    prev: Candle | None = None
    for c in candles:
        if c.open_time in seen:
            dups += 1
        seen.add(c.open_time)
        if prev is not None:
            if c.open_time <= prev.open_time:
                ooo += 1
            else:
                delta = int((c.open_time - prev.open_time).total_seconds() // 60) // step_min
                if delta > 1 and len(gaps) < max_gaps_listed:
                    gaps.append((prev.open_time, c.open_time, delta - 1))
        if not (c.low <= c.open <= c.high and c.low <= c.close <= c.high):
            ohlc_bad += 1
        prev = c
    first, last = candles[0].open_time, candles[-1].open_time
    expected = int((last - first).total_seconds() // 60) // step_min + 1
    return ValidationReport(
        count=len(candles),
        first=first,
        last=last,
        expected_minutes=expected,
        missing_minutes=max(0, expected - len(seen)),
        duplicates=dups,
        out_of_order=ooo,
        gaps=gaps,
        ohlc_violations=ohlc_bad,
    )
