"""Programmatic Alembic helpers (run in a worker thread; Alembic is synchronous)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine as create_sync_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def sync_url(url: str) -> str:
    return url.replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_url(url))
    return cfg


def head_revision() -> str:
    script = ScriptDirectory.from_config(_config("sqlite://"))
    head = script.get_current_head()
    return head or ""


def current_revision(url: str) -> str | None:
    engine = create_sync_engine(sync_url(url))
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


async def stamp_head(url: str) -> None:
    await asyncio.to_thread(command.stamp, _config(url), "head")


async def upgrade_head(url: str) -> None:
    await asyncio.to_thread(command.upgrade, _config(url), "head")


async def schema_status(url: str) -> tuple[str | None, str]:
    """(current, head) — current is None for a pre-alembic or empty DB."""
    cur = await asyncio.to_thread(current_revision, url)
    return cur, head_revision()
