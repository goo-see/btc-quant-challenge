"""Sync KRW-BTC 1m candles from Upbit into the DB (resumable) and validate.

usage: uv run python ../scripts/fetch_history.py [--years 2] [--start YYYY-MM-DD] [--validate-only]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.logging import configure_logging, get_logger
from app.core.types import Timeframe
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.rest_client import UpbitRestClient
from app.market.historical import HistoricalSync, validate_series

log = get_logger("fetch_history")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None)
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument(
        "--tf",
        default="1m",
        choices=["1m", "1h"],
        help="1m (recent, source of truth) or 1h (long history)",
    )
    args = ap.parse_args()

    settings = Settings.load()
    configure_logging(settings.config.system.log_level, json_output=False)
    market = settings.config.system.market
    years = args.years or settings.config.market_data.history_years
    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        if args.start
        else (datetime.now(UTC) - timedelta(days=365 * years)).replace(second=0, microsecond=0)
    )

    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)

    tf = Timeframe(args.tf)
    if not args.validate_only:
        async with UpbitRestClient() as client:
            report = await HistoricalSync(client, repo).sync(market, start, tf=tf)
            print(
                f"\nSYNC  requests={report.requests} fetched={report.fetched} "
                f"db_count={report.db_count} range={report.db_min} .. {report.db_max}"
            )
            m = client.metrics
            print(
                f"REST  latency_ema={m.latency_ema_ms:.0f}ms errors={m.errors} rate_limited={m.rate_limited} last_remaining_req={m.last_remaining_req!r}"
            )

    candles = await repo.load_candles(market, tf)
    v = validate_series(candles)
    print(
        f"\nVALIDATE tf={tf.value} count={v.count} first={v.first} last={v.last}\n"
        f"  expected_bars={v.expected_minutes} missing={v.missing_minutes} coverage={v.coverage_pct:.3f}%\n"
        f"  duplicates={v.duplicates} out_of_order={v.out_of_order} ohlc_violations={v.ohlc_violations}\n"
        f"  gaps(first {len(v.gaps)}): "
        + ", ".join(f"{a:%m-%d %H:%M}->{b:%m-%d %H:%M}({n}m)" for a, b, n in v.gaps[:10])
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
