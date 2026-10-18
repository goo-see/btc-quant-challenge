"""Upbit public WebSocket client (wss://api.upbit.com/websocket/v1).

- subscribe to ticker / trade / candle.{unit}m for KRW-BTC
- exponential backoff reconnect (initial..max, jitter), respects 5 connects/sec
- server drops idle connections after 120s -> we send PING every `ping_interval`
- health counters: connected, reconnect_count, message_count, last_message_at, latency_ms
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from app.core.logging import get_logger

log = get_logger("upbit.ws")

PUBLIC_WS_URL = "wss://api.upbit.com/websocket/v1"

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class WsHealth:
    connected: bool = False
    reconnect_count: int = 0
    message_count: int = 0
    last_message_at: datetime | None = None
    last_connect_at: datetime | None = None
    last_disconnect_reason: str | None = None
    latency_ema_ms: float = 0.0  # server `timestamp` vs local receive time
    errors: int = 0

    def seconds_since_last_message(self, now: datetime | None = None) -> float | None:
        if self.last_message_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.last_message_at).total_seconds()


@dataclass(slots=True)
class Subscription:
    type: str  # "ticker" | "trade" | "orderbook" | "candle.1m" ...
    codes: list[str]
    is_only_realtime: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {"type": self.type, "codes": [c.upper() for c in self.codes]}
        if self.is_only_realtime:
            p["is_only_realtime"] = True
        p.update(self.extra)
        return p


class UpbitWebSocketClient:
    def __init__(
        self,
        subscriptions: list[Subscription],
        handler: MessageHandler,
        *,
        url: str = PUBLIC_WS_URL,
        ping_interval: float = 30.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        connect_factory: Callable[[str], Awaitable[ClientConnection]] | None = None,
    ) -> None:
        self._subs = subscriptions
        self._handler = handler
        self._url = url
        self._ping_interval = ping_interval
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._connect = connect_factory or self._default_connect
        self._stop = asyncio.Event()
        self.health = WsHealth()

    @staticmethod
    async def _default_connect(url: str) -> ClientConnection:
        # We manage keepalive ourselves (Upbit expects PING frames or "PING" text).
        return await websockets.connect(url, ping_interval=None, max_size=2**22)

    def stop(self) -> None:
        self._stop.set()

    def subscribe_message(self) -> str:
        return json.dumps(
            [
                {"ticket": str(uuid.uuid4())},
                *[s.to_payload() for s in self._subs],
                {"format": "DEFAULT"},
            ]
        )

    async def run(self) -> None:
        """Run forever (until stop()) with reconnect. Never raises on transient failures."""
        backoff = self._backoff_initial
        first = True
        while not self._stop.is_set():
            try:
                ws = await self._connect(self._url)
            except Exception as exc:
                self.health.errors += 1
                self.health.last_disconnect_reason = f"connect_failed: {exc!r}"
                log.warning("ws_connect_failed", error=repr(exc), backoff=backoff)
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue

            self.health.connected = True
            self.health.last_connect_at = datetime.now(UTC)
            if not first:
                self.health.reconnect_count += 1
                log.info("ws_reconnected", count=self.health.reconnect_count)
            else:
                log.info("ws_connected", url=self._url)
            first = False
            backoff = self._backoff_initial

            try:
                await ws.send(self.subscribe_message())
                await self._consume(ws)
            except (TimeoutError, websockets.ConnectionClosed, OSError) as exc:
                self.health.last_disconnect_reason = repr(exc)
                log.warning("ws_disconnected", error=repr(exc))
            except Exception as exc:
                self.health.errors += 1
                self.health.last_disconnect_reason = f"unexpected: {exc!r}"
                log.error("ws_unexpected_error", error=repr(exc))
            finally:
                self.health.connected = False
                with contextlib.suppress(Exception):
                    await ws.close()
            if not self._stop.is_set():
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self._backoff_max)

    async def _sleep_backoff(self, backoff: float) -> None:
        jitter = random.uniform(0, backoff * 0.3)  # noqa: S311 — not security-related
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=backoff + jitter)

    async def _consume(self, ws: ClientConnection) -> None:
        ping_task = asyncio.create_task(self._ping_loop(ws))
        stop_task = asyncio.create_task(self._stop.wait())
        recv_task: asyncio.Task[str | bytes] | None = None
        try:
            while not self._stop.is_set():
                if recv_task is None:
                    recv_task = asyncio.create_task(ws.recv())
                # If nothing arrives for 2x ping interval + margin, treat the connection as dead.
                done, _ = await asyncio.wait(
                    {recv_task, stop_task},
                    timeout=self._ping_interval * 2 + 10,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return
                if recv_task not in done:
                    raise TimeoutError("ws_recv_timeout")
                raw = recv_task.result()
                recv_task = None
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                if not isinstance(data, dict):
                    continue
                if data.get("status") == "UP":
                    continue  # PING response
                now = time.time() * 1000
                self.health.message_count += 1
                self.health.last_message_at = datetime.now(UTC)
                ts = data.get("timestamp")
                if isinstance(ts, int | float):
                    lat = max(0.0, now - float(ts))
                    self.health.latency_ema_ms = (
                        lat
                        if self.health.latency_ema_ms == 0
                        else self.health.latency_ema_ms * 0.9 + lat * 0.1
                    )
                try:
                    await self._handler(data)
                except Exception as exc:
                    self.health.errors += 1
                    log.error("ws_handler_error", error=repr(exc), type=data.get("type"))
        finally:
            for t in (ping_task, stop_task, recv_task):
                if t is not None:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t

    async def _ping_loop(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send("PING")
