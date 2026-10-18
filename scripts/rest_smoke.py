"""Hit Upbit public REST once (ticker + 3 candles) and, if keys exist, private accounts/chance.

usage: uv run python ../scripts/rest_smoke.py
"""

from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.logging import configure_logging
from app.exchange.upbit.rest_client import UpbitRestClient


async def main() -> None:
    configure_logging("INFO", json_output=False)
    s = Settings.load()
    ak = s.env.upbit_access_key.get_secret_value() if s.env.upbit_access_key else None
    sk = s.env.upbit_secret_key.get_secret_value() if s.env.upbit_secret_key else None
    async with UpbitRestClient(ak, sk) as c:
        t = await c.get_ticker("KRW-BTC")
        print(
            f"ticker  price={t.trade_price} ts={t.trade_timestamp} 24h_vol={t.acc_trade_volume_24h}"
        )
        cs = await c.get_minute_candles("KRW-BTC", 1, count=3)
        for k in cs:
            print(
                f"candle  {k.candle_date_time_utc}Z o={k.opening_price} h={k.high_price} l={k.low_price} c={k.trade_price} v={k.candle_acc_trade_volume}"
            )
        print(
            f"metrics requests={c.metrics.requests} latency_ema={c.metrics.latency_ema_ms:.0f}ms remaining_req={c.metrics.last_remaining_req!r}"
        )
        if s.env.has_credentials:
            accts = await c.get_accounts()
            print("accounts", [(a.currency, str(a.balance), str(a.locked)) for a in accts])
            ch = await c.get_order_chance("KRW-BTC")
            print(
                f"chance  bid_fee={ch.bid_fee} ask_fee={ch.ask_fee} min_total bid/ask={ch.market.bid.min_total}/{ch.market.ask.min_total} max_total={ch.market.max_total} state={ch.market.state}"
            )
        else:
            print("no credentials in .env — private endpoints skipped (expected on Day 1)")


if __name__ == "__main__":
    asyncio.run(main())
