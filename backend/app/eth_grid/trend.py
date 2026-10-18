"""Daily EMA100 trend guard for the ETH grid — same inputs as the research harness.

Research: daily buckets (UTC days) resampled from 5m bars, EMA100 seeded over the full history,
`trend_ok = daily close > EMA` evaluated when a day closes. Here the daily closes come from the
read-only eth_market.db (2017-09-30 →) and are extended with public REST 4h candles for the days
after the DB's last complete day; the EMA is recomputed over the full series each refresh.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.market.indicators import EMA


@dataclass(frozen=True, slots=True)
class TrendReading:
    ok: bool
    last_day: date | None
    daily_close: Decimal | None
    ema: Decimal | None
    days: int


class DailyTrendGuard:
    def __init__(self, period: int = 100, *, market: str = "KRW-ETH") -> None:
        self.period = period
        self.market = market
        self.closes: dict[date, Decimal] = {}
        self.last: TrendReading = TrendReading(True, None, None, None, 0)

    # ------------------------------------------------------------------ inputs
    def load_db(self, path: Path, now: datetime | None = None) -> int:
        """Daily closes (UTC days) from the 5m table; the current (incomplete) day is skipped."""
        if not path.exists():
            return 0
        now = now or datetime.now(UTC)
        today = now.date()
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute(
                "select substr(open_time,1,10) as d, close from candles "
                "where market=? and timeframe='5m' and open_time in "
                "(select max(open_time) from candles where market=? and timeframe='5m' "
                " group by substr(open_time,1,10)) order by d",
                (self.market, self.market),
            ).fetchall()
        finally:
            con.close()
        n = 0
        for d, close in rows:
            day = date.fromisoformat(str(d))
            if day >= today:
                continue
            self.closes[day] = Decimal(str(close))
            n += 1
        return n

    def add_4h_candles(self, candles: list[tuple[datetime, Decimal]], now: datetime) -> int:
        """(open_time UTC, close) 4h bars -> daily closes for COMPLETE days (now >= day end)."""
        by_day: dict[date, tuple[datetime, Decimal]] = {}
        for open_time, close in candles:
            d = open_time.astimezone(UTC).date()
            cur = by_day.get(d)
            if cur is None or open_time > cur[0]:
                by_day[d] = (open_time, close)
        n = 0
        for d, (open_time, close) in by_day.items():
            day_end = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
            if now < day_end + timedelta(minutes=1):
                continue  # day still running
            if open_time.hour != 20:
                continue  # the 20:00-24:00 bar is the daily close; anything else is incomplete
            self.closes[d] = close
            n += 1
        return n

    # ------------------------------------------------------------------ evaluation
    def evaluate(self) -> TrendReading:
        if not self.closes:
            self.last = TrendReading(True, None, None, None, 0)
            return self.last
        ema = EMA(self.period)
        v: float | None = None
        last_day = max(self.closes)
        for d in sorted(self.closes):
            v = ema.update(float(self.closes[d]))
        close = self.closes[last_day]
        ema_d = Decimal(str(round(v, 2))) if v is not None else None
        self.last = TrendReading(
            ema_d is None or close > ema_d, last_day, close, ema_d, len(self.closes)
        )
        return self.last
