"""Private API preflight: credentials -> static IP gate -> accounts -> orders/chance -> orders/test.

Never creates a real order. Records the outcome in bot_state.order_test (readiness panel).
usage: uv run python ../scripts/private_smoke.py
"""

from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.logging import configure_logging
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.services.live_preflight import run_preflight


async def main() -> None:
    settings = Settings.load()
    configure_logging("WARNING", json_output=False)
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    res = await run_preflight(settings, repo)
    print("PRIVATE API PREFLIGHT")
    for s in res.steps:
        print(f"  {'PASS' if s['ok'] else 'FAIL':<5} {s['name']:<18} {s['detail']}")
    print(
        f"result: {'OK' if res.ok else 'BLOCKED'}{'' if res.ok else ' reason=' + str(res.blocked_reason)}"
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
