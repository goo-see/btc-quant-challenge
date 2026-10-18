"""Generate the daily report for a KST date (default: today so far).

usage: uv run python ../scripts/daily_report.py [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.monitoring.daily_report import build_daily_report, save_daily_report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    settings = Settings.load()
    cfg = settings.config
    day = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(ZoneInfo(cfg.system.report_timezone)).date()
    )
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    bot = await repo.get_state("bot") or {}
    mode = TradingMode(bot.get("mode") or settings.mode.value)
    ch = await repo.get_state("challenge") or {}
    start = ch.get("start_date") or cfg.challenge.start_date
    rep = await build_daily_report(
        repo,
        mode=mode,
        day=day,
        tz=cfg.system.report_timezone,
        initial_equity=cfg.challenge.initial_capital_krw,
        challenge_start=date.fromisoformat(start) if start else None,
    )
    path = await save_daily_report(repo, rep, cfg.system.reports_dir)
    print(path)
    print(rep.to_markdown())
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
