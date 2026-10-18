"""Fetch the full Upbit KRW-ETH 5m history into a SEPARATE research DB (data/eth_market.db).

usage: uv run python ../scripts/fetch_eth_history.py [--start 2017-09-30] [--tf 5m] [--db ../data/eth_market.db]
Public REST only (no credentials); production quant.db is never touched. Resumable (upsert by open_time).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

import _bootstrap  # noqa: F401

from app.core.logging import configure_logging
from app.core.types import Timeframe
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.rest_client import UpbitRestClient
from app.market.historical import HistoricalSync, validate_series


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-09-30")
    ap.add_argument("--tf", default="5m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--db", default="../data/eth_market.db")
    ap.add_argument("--market", default="KRW-ETH")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    configure_logging("WARNING", json_output=False)
    engine = create_engine(f"sqlite+aiosqlite:///{args.db}")
    await init_schema(engine)
    repo = Repository(engine)
    tf = Timeframe(args.tf)
    if not args.validate_only:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=tf.minutes)
        t0 = datetime.now(UTC)
        async with UpbitRestClient() as rest:
            n = await HistoricalSync(rest, repo).sync(args.market, start, end, tf=tf)
        print(
            f"synced {n} {args.tf} candles for {args.market} in {(datetime.now(UTC) - t0).total_seconds():.0f}s"
        )
    candles = await repo.load_candles(args.market, tf)
    v = validate_series(candles)
    print(
        f"{args.market} {args.tf}: {v.count} bars {v.first} -> {v.last}; coverage {v.coverage_pct:.3f}% missing {v.missing_minutes} dup {v.duplicates} ohlc_violations {v.ohlc_violations}; largest gaps {[(str(a)[:16], str(b)[:16], n) for a, b, n in sorted(v.gaps, key=lambda g: -g[2])[:5]]}"
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
