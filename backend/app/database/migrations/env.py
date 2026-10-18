"""Alembic environment. Uses a SYNC engine (aiosqlite URL is rewritten to plain sqlite)."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.database.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("ALEMBIC_DATABASE_URL")
    if not url:
        import yaml

        cfg_path = Path(os.environ.get("CONFIG_PATH", "../config/config.yaml"))
        with cfg_path.open(encoding="utf-8") as f:
            url = str(yaml.safe_load(f)["system"]["database_url"])
    return url.replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # render_as_batch: SQLite cannot ALTER most things; batch mode recreates tables safely.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
