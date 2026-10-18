"""Connect to Upbit public WebSocket for N seconds and print health counters.

usage: uv run python ../scripts/ws_smoke.py [--seconds 60]
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import _bootstrap  # noqa: F401

from app.core.logging import configure_logging
from app.exchange.upbit.ws_client import Subscription, UpbitWebSocketClient

counts: dict[str, int] = {}
last: dict[str, Any] = {}


async def handler(msg: dict[str, Any]) -> None:
    t = str(msg.get("type"))
    counts[t] = counts.get(t, 0) + 1
    last[t] = msg


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()
    configure_logging("INFO", json_output=False)
    client = UpbitWebSocketClient(
        [
            Subscription("ticker", ["KRW-BTC"]),
            Subscription("trade", ["KRW-BTC"]),
            Subscription("candle.1m", ["KRW-BTC"]),
        ],
        handler,
        ping_interval=20.0,
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(args.seconds)
    client.stop()
    await asyncio.wait_for(task, timeout=10)
    h = client.health
    print("\nWS SMOKE RESULT")
    print(f"  connected_at={h.last_connect_at} reconnects={h.reconnect_count} errors={h.errors}")
    print(f"  messages={h.message_count} by_type={counts} latency_ema={h.latency_ema_ms:.0f}ms")
    print(
        f"  last_message_at={h.last_message_at} last_disconnect_reason={h.last_disconnect_reason}"
    )
    tk = last.get("ticker", {})
    cd = last.get("candle.1m", {})
    print(f"  ticker.trade_price={tk.get('trade_price')} stream_type={tk.get('stream_type')}")
    print(
        f"  candle.1m utc={cd.get('candle_date_time_utc')} o/h/l/c={cd.get('opening_price')}/{cd.get('high_price')}/{cd.get('low_price')}/{cd.get('trade_price')}"
    )


if __name__ == "__main__":
    asyncio.run(main())
