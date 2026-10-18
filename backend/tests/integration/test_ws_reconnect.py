"""WebSocket client reconnect behaviour with a fake connection factory (no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from app.exchange.upbit.ws_client import Subscription, UpbitWebSocketClient


class FakeConn:
    def __init__(self, messages: list[str], *, fail_after: bool) -> None:
        self._messages = list(messages)
        self._fail_after = fail_after
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._messages:
            await asyncio.sleep(0)
            return self._messages.pop(0)
        if self._fail_after:
            raise websockets.ConnectionClosedError(None, None)
        await asyncio.sleep(3600)
        return ""

    async def close(self) -> None:
        self.closed = True


def _msg(price: float, ts: int) -> str:
    return json.dumps({"type": "ticker", "code": "KRW-BTC", "trade_price": price, "timestamp": ts})


async def test_reconnects_with_backoff_and_resubscribes() -> None:
    conns: list[FakeConn] = []
    attempts = {"n": 0}

    async def factory(_url: str) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise OSError("dns down")  # connect failure path
        c = FakeConn([_msg(1.0, 1), '{"status":"UP"}', _msg(2.0, 2)], fail_after=attempts["n"] < 3)
        conns.append(c)
        return c

    received: list[dict[str, Any]] = []

    async def handler(m: dict[str, Any]) -> None:
        received.append(m)

    client = UpbitWebSocketClient(
        [Subscription("ticker", ["krw-btc"])],
        handler,
        backoff_initial=0.01,
        backoff_max=0.05,
        ping_interval=0.02,
        connect_factory=factory,
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.5)
    client.stop()
    await asyncio.wait_for(task, 2)

    assert attempts["n"] >= 3
    assert client.health.reconnect_count >= 1
    assert client.health.errors >= 1  # the OSError connect failure
    assert client.health.message_count == 4  # 2 tickers x 2 successful connections ("UP" excluded)
    assert len(received) == 4
    # every connection received a subscribe message with uppercase codes
    for c in conns:
        sub = json.loads(c.sent[0])
        assert (
            sub[0]["ticket"]
            and sub[1] == {"type": "ticker", "codes": ["KRW-BTC"]}
            and sub[-1] == {"format": "DEFAULT"}
        )
    # keepalive PINGs were sent on the long-lived connection
    assert "PING" in conns[-1].sent
    assert client.health.connected is False


async def test_handler_exception_does_not_kill_feed() -> None:
    async def factory(_url: str) -> Any:
        return FakeConn([_msg(1.0, 1), _msg(2.0, 2)], fail_after=False)

    seen = {"n": 0}

    async def bad_handler(m: dict[str, Any]) -> None:
        seen["n"] += 1
        raise RuntimeError("bug")

    client = UpbitWebSocketClient(
        [Subscription("ticker", ["KRW-BTC"])], bad_handler, connect_factory=factory, ping_interval=1
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.2)
    client.stop()
    await asyncio.wait_for(task, 2)
    assert seen["n"] == 2 and client.health.errors == 2 and client.health.message_count == 2
