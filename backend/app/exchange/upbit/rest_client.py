"""Async Upbit REST client with auth, per-group rate limiting, retries and latency metrics.

Public: candles/ticker. Private: accounts, orders/chance, order lookup/cancel, open/closed orders,
orders/test (dry run) and create_order (REAL MONEY — only reachable through LiveGateway).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.core.logging import get_logger
from app.exchange.upbit.auth import auth_header, build_query_string
from app.exchange.upbit.errors import (
    UpbitAuthError,
    UpbitCredentialsMissingError,
    UpbitHTTPError,
    UpbitNetworkError,
    UpbitRateLimitedError,
)
from app.exchange.upbit.models import (
    UpbitAccount,
    UpbitCandle,
    UpbitOrder,
    UpbitOrderChance,
    UpbitTicker,
)
from app.exchange.upbit.rate_limiter import RateLimiter

log = get_logger("upbit.rest")

BASE_URL = "https://api.upbit.com"
CANDLE_MAX_COUNT = 200


@dataclass(slots=True)
class RestMetrics:
    requests: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    rate_limited: int = 0
    latency_ema_ms: float = 0.0
    last_error: str | None = None
    last_remaining_req: str | None = None
    by_status: dict[int, int] = field(default_factory=dict)

    def record_latency(self, ms: float) -> None:
        self.latency_ema_ms = (
            ms if self.latency_ema_ms == 0 else self.latency_ema_ms * 0.8 + ms * 0.2
        )


class UpbitRestClient:
    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._limiter = rate_limiter or RateLimiter()
        self._max_retries = max_retries
        self.metrics = RestMetrics()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> UpbitRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    def _auth(self, params: Mapping[str, Any] | None) -> dict[str, str]:
        if not self._access_key or not self._secret_key:
            raise UpbitCredentialsMissingError(
                "private endpoint requires UPBIT_ACCESS_KEY/SECRET_KEY"
            )
        return auth_header(self._access_key, self._secret_key, params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        private: bool = False,
        retry_idempotent: bool = True,
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire(group)
            headers: dict[str, str] = {"Accept": "application/json"}
            query = build_query_string(params) if params else None
            if private:
                headers.update(self._auth(json_body if json_body is not None else params))
            url = f"{path}?{query}" if query else path
            started = time.perf_counter()
            try:
                resp = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=dict(json_body) if json_body is not None else None,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self._on_error(repr(exc))
                if retry_idempotent and attempt <= self._max_retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise UpbitNetworkError(str(exc)) from exc

            latency_ms = (time.perf_counter() - started) * 1000
            self.metrics.requests += 1
            self.metrics.record_latency(latency_ms)
            self.metrics.by_status[resp.status_code] = (
                self.metrics.by_status.get(resp.status_code, 0) + 1
            )
            remaining = resp.headers.get("Remaining-Req")
            self.metrics.last_remaining_req = remaining
            self._limiter.feedback(remaining, resp.status_code)

            if resp.status_code in (429, 418):
                self.metrics.rate_limited += 1
                self._on_error(f"rate_limited {resp.status_code}")
                log.warning(
                    "upbit_rate_limited", status=resp.status_code, group=group, remaining=remaining
                )
                if attempt <= self._max_retries:
                    await asyncio.sleep(1.0 if resp.status_code == 429 else 60.0)
                    continue
                raise UpbitRateLimitedError(resp.status_code, "rate_limited", None, resp.text)

            if resp.status_code >= 500:
                self._on_error(f"server_error {resp.status_code}")
                if retry_idempotent and attempt <= self._max_retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise UpbitHTTPError(resp.status_code, "server_error", None, resp.text)

            if resp.status_code >= 400:
                name, message = _parse_error(resp.text)
                self._on_error(f"{resp.status_code} {name}")
                if resp.status_code in (401, 403):
                    raise UpbitAuthError(resp.status_code, name, message, resp.text)
                raise UpbitHTTPError(resp.status_code, name, message, resp.text)

            self.metrics.consecutive_errors = 0
            return resp.json()

    def _on_error(self, msg: str) -> None:
        self.metrics.errors += 1
        self.metrics.consecutive_errors += 1
        self.metrics.last_error = msg

    # ---------------------------------------------------------------- public
    async def get_minute_candles(
        self, market: str, unit: int, *, count: int = CANDLE_MAX_COUNT, to: datetime | None = None
    ) -> list[UpbitCandle]:
        if unit not in (1, 3, 5, 10, 15, 30, 60, 240):
            raise ValueError(f"unsupported candle unit {unit}")
        params: dict[str, Any] = {"market": market, "count": min(count, CANDLE_MAX_COUNT)}
        if to is not None:
            params["to"] = to.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._request(
            "GET", f"/v1/candles/minutes/{unit}", group="candle", params=params
        )
        return [UpbitCandle.model_validate(c) for c in data]

    async def get_ticker(self, market: str) -> UpbitTicker:
        data = await self._request("GET", "/v1/ticker", group="ticker", params={"markets": market})
        return UpbitTicker.model_validate(data[0])

    # --------------------------------------------------------------- private
    async def get_accounts(self) -> list[UpbitAccount]:
        data = await self._request("GET", "/v1/accounts", group="default", private=True)
        return [UpbitAccount.model_validate(a) for a in data]

    async def get_order_chance(self, market: str) -> UpbitOrderChance:
        data = await self._request(
            "GET", "/v1/orders/chance", group="default", params={"market": market}, private=True
        )
        return UpbitOrderChance.model_validate(data)

    async def get_order(
        self, *, uuid: str | None = None, identifier: str | None = None
    ) -> UpbitOrder:
        if not uuid and not identifier:
            raise ValueError("uuid or identifier required")
        params: dict[str, Any] = {}
        if uuid:
            params["uuid"] = uuid
        elif identifier:
            params["identifier"] = identifier
        data = await self._request("GET", "/v1/order", group="default", params=params, private=True)
        return UpbitOrder.model_validate(data)

    def _order_body(
        self,
        *,
        market: str,
        side: str,
        ord_type: str,
        price: Decimal | None,
        volume: Decimal | None,
        identifier: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"market": market, "side": side, "ord_type": ord_type}
        if price is not None:
            body["price"] = f"{price:f}"
        if volume is not None:
            body["volume"] = f"{volume:f}"
        if identifier:
            body["identifier"] = identifier
        return body

    async def test_order(
        self,
        *,
        market: str,
        side: str,
        ord_type: str,
        price: Decimal | None = None,
        volume: Decimal | None = None,
        identifier: str | None = None,
    ) -> UpbitOrder:
        """POST /v1/orders/test — validated like a real order, never executed."""
        body = self._order_body(
            market=market,
            side=side,
            ord_type=ord_type,
            price=price,
            volume=volume,
            identifier=identifier,
        )
        data = await self._request(
            "POST",
            "/v1/orders/test",
            group="order-test",
            json_body=body,
            private=True,
            retry_idempotent=False,
        )
        return UpbitOrder.model_validate(data)

    async def create_order(
        self,
        *,
        market: str,
        side: str,
        ord_type: str,
        price: Decimal | None = None,
        volume: Decimal | None = None,
        identifier: str | None = None,
    ) -> UpbitOrder:
        """POST /v1/orders — REAL order. Never retried; idempotency comes from `identifier`."""
        body = self._order_body(
            market=market,
            side=side,
            ord_type=ord_type,
            price=price,
            volume=volume,
            identifier=identifier,
        )
        data = await self._request(
            "POST",
            "/v1/orders",
            group="order",
            json_body=body,
            private=True,
            retry_idempotent=False,
        )
        return UpbitOrder.model_validate(data)

    async def cancel_order(
        self, *, uuid: str | None = None, identifier: str | None = None
    ) -> UpbitOrder:
        params: dict[str, Any] = {}
        if uuid:
            params["uuid"] = uuid
        elif identifier:
            params["identifier"] = identifier
        else:
            raise ValueError("uuid or identifier required")
        data = await self._request(
            "DELETE",
            "/v1/order",
            group="default",
            params=params,
            private=True,
            retry_idempotent=False,
        )
        return UpbitOrder.model_validate(data)

    async def get_open_orders(
        self, market: str, states: tuple[str, ...] = ("wait",)
    ) -> list[UpbitOrder]:
        params: dict[str, Any] = {"market": market, "states": list(states), "limit": 100}
        data = await self._request(
            "GET", "/v1/orders/open", group="default", params=params, private=True
        )
        return [UpbitOrder.model_validate(o) for o in data]


def _parse_error(body: str) -> tuple[str | None, str | None]:
    try:
        obj = json.loads(body)
        err = obj.get("error", {}) if isinstance(obj, dict) else {}
        return err.get("name"), err.get("message")
    except (json.JSONDecodeError, AttributeError):
        return None, None
