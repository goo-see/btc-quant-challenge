"""Baseline migration must (a) build a fresh schema and (b) adopt a pre-alembic DB without data loss."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.database.engine import create_engine, init_schema
from app.database.migrate import head_revision
from app.database.repository import Repository
from tests.conftest import synthetic_1m

BACKEND = Path(__file__).resolve().parents[2]
REAL_DB = BACKEND.parent / "data" / "quant.db"


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": f"sqlite:///{db}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _q(db: Path, sql: str) -> str:
    import sqlite3

    with sqlite3.connect(db) as c:
        return str(c.execute(sql).fetchone()[0])


def test_fresh_db_upgrade_head_and_no_drift(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert _q(db, "select version_num from alembic_version") == head_revision()
    assert int(_q(db, "select count(*) from sqlite_master where type='table'")) >= 15
    r = _alembic(db, "check")
    assert r.returncode == 0 and "No new upgrade operations detected" in (r.stdout + r.stderr)


async def test_pre_alembic_db_is_adopted_without_data_loss(tmp_path: Path) -> None:
    """A Day-1 DB (schema of revision 0001, created by create_all, no alembic_version)."""
    db = tmp_path / "legacy.db"
    r = _alembic(db, "upgrade", "0001")
    assert r.returncode == 0, r.stderr
    import sqlite3

    with sqlite3.connect(db) as c:
        c.execute("drop table alembic_version")  # -> pre-alembic
    engine = create_engine(f"sqlite+aiosqlite:///{db}")
    repo = Repository(engine)
    await repo.upsert_candles(synthetic_1m(1000))
    await engine.dispose()
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert _q(db, "select version_num from alembic_version") == head_revision()
    assert _q(db, "select count(*) from candles") == "1000"
    assert "command_id" in _q(db, "select group_concat(name) from pragma_table_info('commands')")


async def test_init_schema_on_empty_db_stamps_head(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db}")
    await init_schema(engine)
    await engine.dispose()
    assert _q(db, "select version_num from alembic_version") == head_revision()
    assert _alembic(db, "check").returncode == 0


@pytest.mark.skipif(not REAL_DB.exists(), reason="historical quant.db not present")
def test_real_historical_db_copy_migrates(tmp_path: Path) -> None:
    db = tmp_path / "quant_copy.db"
    shutil.copy(REAL_DB, db)
    before = _q(db, "select count(*) from candles")
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert _q(db, "select count(*) from candles") == before
    assert _alembic(db, "check").returncode == 0
