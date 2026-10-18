"""Synchronous READ-ONLY access to the canonical market DB (candles table only).

The connection is opened with `?mode=ro` so any write attempt raises sqlite3.OperationalError
("attempt to write a readonly database") — enforced by tests/unit/test_round3_no_live_order.py.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.types import Candle, Timeframe


def _fmt(d: datetime) -> str:
    return d.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


class MarketReader:
    def __init__(self, db_path: Path, market: str) -> None:
        self.db_path = db_path
        self.market = market
        self._con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._con

    def close(self) -> None:
        self._con.close()

    def load(
        self,
        tf: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        after: datetime | None = None,
    ) -> list[Candle]:
        """Candles with start <= open_time < end (and open_time > after), ascending."""
        sql = "select open_time, open, high, low, close, volume, quote_volume from candles where market=? and timeframe=?"
        args: list[str] = [self.market, tf.value]
        if start is not None:
            sql += " and open_time >= ?"
            args.append(_fmt(start))
        if after is not None:
            sql += " and open_time > ?"
            args.append(_fmt(after))
        if end is not None:
            sql += " and open_time < ?"
            args.append(_fmt(end))
        sql += " order by open_time"
        rows = self._con.execute(sql, args).fetchall()
        return [
            Candle(
                market=self.market,
                timeframe=tf,
                open_time=_parse(r[0]),
                open=Decimal(str(r[1])),
                high=Decimal(str(r[2])),
                low=Decimal(str(r[3])),
                close=Decimal(str(r[4])),
                volume=Decimal(str(r[5])),
                quote_volume=Decimal(str(r[6])),
            )
            for r in rows
        ]

    def latest_open(self, tf: Timeframe) -> datetime | None:
        row = self._con.execute(
            "select max(open_time) from candles where market=? and timeframe=?",
            (self.market, tf.value),
        ).fetchone()
        return _parse(row[0]) if row and row[0] else None

    def range_stats(
        self, tf: Timeframe, start: datetime, end: datetime
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        """(max high, min low, last close) over start <= open_time < end — for MFE/MAE."""
        row = self._con.execute(
            "select max(high), min(low) from candles where market=? and timeframe=? and open_time >= ? and open_time < ?",
            (self.market, tf.value, _fmt(start), _fmt(end)),
        ).fetchone()
        last = self._con.execute(
            "select close from candles where market=? and timeframe=? and open_time >= ? and open_time < ? order by open_time desc limit 1",
            (self.market, tf.value, _fmt(start), _fmt(end)),
        ).fetchone()
        return (
            Decimal(str(row[0])) if row and row[0] is not None else None,
            Decimal(str(row[1])) if row and row[1] is not None else None,
            Decimal(str(last[0])) if last else None,
        )

    def close_at(self, tf: Timeframe, open_time: datetime) -> Decimal | None:
        row = self._con.execute(
            "select close from candles where market=? and timeframe=? and open_time=?",
            (self.market, tf.value, _fmt(open_time)),
        ).fetchone()
        return Decimal(str(row[0])) if row else None

    def bot_state(self, key: str) -> str | None:
        row = self._con.execute("select value from bot_state where key=?", (key,)).fetchone()
        return str(row[0]) if row else None
