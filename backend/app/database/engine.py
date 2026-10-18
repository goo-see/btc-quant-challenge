"""Async engine factory. SQLite runs in WAL mode with busy_timeout; writes retry on lock.

Two processes (trader, api) share the file: WAL lets readers proceed during writes, and
`busy_timeout` + `with_lock_retry` cover the rare writer/writer collision.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logging import get_logger
from app.database.models import Base

log = get_logger("db")


SQLITE_BUSY_TIMEOUT_MS = 5000


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    if _is_sqlite(database_url):
        # Ensure parent dir exists for file-based sqlite
        path_part = database_url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            database_url, echo=echo, connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


async def init_schema(engine: AsyncEngine) -> None:
    """Create the schema on an EMPTY database and stamp it at the Alembic head.

    Existing databases are left untouched: run `alembic upgrade head` (the trader/api refuse to
    silently mutate a populated DB). A populated DB behind head is logged as a warning.
    """
    from app.database.migrate import schema_status, stamp_head  # local import: alembic is heavy

    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    url = str(engine.url.render_as_string(hide_password=False))
    if not tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await stamp_head(url)
        return
    current, head = await schema_status(url)
    if current != head:
        log.warning(
            "schema_not_at_head", current=current, head=head, hint="run: alembic upgrade head"
        )


async def check_connection(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.error("db_check_failed", error=repr(exc))
        return False


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, OperationalError) and ("locked" in msg or "busy" in msg)


async def with_lock_retry[T](
    fn: Callable[[], Awaitable[T]], *, retries: int = 5, base_delay: float = 0.05
) -> T:
    """Retry a DB operation on SQLite lock contention with jittered exponential backoff."""
    attempt = 0
    while True:
        try:
            return await fn()
        except OperationalError as exc:
            attempt += 1
            if not is_lock_error(exc) or attempt > retries:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)  # noqa: S311
            log.warning("db_locked_retry", attempt=attempt, delay=round(delay, 3))
            await asyncio.sleep(delay)
