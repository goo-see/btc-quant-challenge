"""Run the deterministic replay/synthetic E2E suite for the active strategy and record the result
into bot_state.replay_e2e (readiness evidence). Uses temp databases; never touches the paper books.

usage: uv run python ../scripts/replay_e2e.py [--only 01_...,02_...]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.logging import configure_logging
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.validation.replay_scenarios import run_all


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    configure_logging("ERROR", json_output=False)
    results = await run_all(set(args.only.split(",")) if args.only else None)
    passed = sum(1 for r in results if r.ok)
    print(f"REPLAY E2E: {passed}/{len(results)} passed")
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.name:<48} {r.detail}")
    settings = Settings.load()
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    await repo.set_state(
        "replay_e2e",
        {
            "ok": passed == len(results) and len(results) >= 20,
            "passed": passed,
            "total": len(results),
            "at": datetime.now(UTC).isoformat(),
            "strategy_version": settings.config.strategy.strategy_version,
            "results": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results],
        },
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
