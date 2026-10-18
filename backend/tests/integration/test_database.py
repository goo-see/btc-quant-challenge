"""SQLite WAL, repository CRUD, concurrent writers from two engines (lock/retry policy)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.types import Timeframe
from app.database.engine import (
    check_connection,
    create_engine,
    init_schema,
    is_lock_error,
    with_lock_retry,
)
from app.database.repository import Repository
from tests.conftest import synthetic_1m


async def test_wal_mode_and_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite")
    await init_schema(engine)
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        tables = {
            r[0]
            for r in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert mode == "wal"
    assert {
        "candles",
        "orders",
        "fills",
        "trades",
        "signals",
        "risk_decisions",
        "events",
        "bot_state",
        "commands",
        "portfolio_snapshots",
        "backtest_runs",
        "reconciliations",
        "daily_stats",
        "health_snapshots",
    } <= tables
    assert await check_connection(engine)
    await engine.dispose()


async def test_candle_upsert_idempotent_and_range(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite")
    await init_schema(engine)
    repo = Repository(engine)
    candles = synthetic_1m(300)
    await repo.upsert_candles(candles)
    await repo.upsert_candles(candles[100:200])  # duplicates ignored
    mn, mx, cnt = await repo.candle_range("KRW-BTC", Timeframe.M1)
    assert cnt == 300 and mn == candles[0].open_time and mx == candles[-1].open_time
    loaded = await repo.load_candles(
        "KRW-BTC", Timeframe.M1, candles[10].open_time, candles[20].open_time
    )
    assert (
        len(loaded) == 10
        and loaded[0].open == candles[10].open
        and isinstance(loaded[0].open, Decimal)
    )
    assert loaded[0].open_time.tzinfo is not None
    await engine.dispose()


async def test_two_engines_concurrent_writes_succeed(tmp_path: Path) -> None:
    """Simulates trader + api processes writing to the same file."""
    url = f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"
    e1, e2 = create_engine(url), create_engine(url)
    await init_schema(e1)
    r1, r2 = Repository(e1), Repository(e2)

    async def writer(repo: Repository, tag: str) -> None:
        for i in range(30):
            await repo.add_event("INFO", "HEARTBEAT", f"{tag}-{i}")

    await asyncio.gather(writer(r1, "a"), writer(r2, "b"), r1.upsert_candles(synthetic_1m(500)))
    async with e1.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM events"))).scalar()
    assert n == 60 and r1.write_errors == 0 and r2.write_errors == 0
    await e1.dispose()
    await e2.dispose()


async def test_with_lock_retry_retries_lock_errors_only() -> None:
    calls = {"n": 0}

    async def locked_then_ok() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return "ok"

    assert await with_lock_retry(locked_then_ok, retries=5, base_delay=0.001) == "ok"
    assert calls["n"] == 3

    async def other_error() -> str:
        raise OperationalError("stmt", {}, Exception("no such table"))

    try:
        await with_lock_retry(other_error, retries=5, base_delay=0.001)
    except OperationalError as exc:
        assert not is_lock_error(exc)
    else:
        raise AssertionError("should raise")
