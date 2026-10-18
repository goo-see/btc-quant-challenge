"""LiveGateway against a mocked Upbit private API (respx). No network, no real orders."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.core.types import OrderState, Side
from app.exchange.base import (
    DuplicateIdentifierError,
    GatewayNetworkError,
    GatewayRejectedError,
    OrderNotFoundError,
)
from app.exchange.upbit.live_gateway import LiveGateway, to_gateway_order
from app.exchange.upbit.models import UpbitOrder
from app.exchange.upbit.rate_limiter import RateLimiter
from app.exchange.upbit.rest_client import UpbitRestClient

BASE = "https://api.upbit.com"


def _order(
    state: str = "done", executed: str = "0.00026", identifier: str = "cid-1"
) -> dict[str, Any]:
    return {
        "uuid": "u-1",
        "side": "bid",
        "ord_type": "price",
        "price": "30000",
        "state": state,
        "market": "KRW-BTC",
        "created_at": "2026-09-25T14:00:00+09:00",
        "volume": None,
        "remaining_volume": None,
        "reserved_fee": "15",
        "remaining_fee": "0",
        "paid_fee": "15",
        "locked": "0",
        "executed_volume": executed,
        "trades_count": 1,
        "identifier": identifier,
        "trades": [
            {
                "market": "KRW-BTC",
                "uuid": "t-1",
                "price": "115000000",
                "volume": executed,
                "funds": "29900",
                "trend": "up",
                "created_at": "2026-09-25T14:00:00+09:00",
                "side": "bid",
            }
        ],
    }


def _gw() -> LiveGateway:
    c = UpbitRestClient(
        "AK",
        "SK",
        rate_limiter=RateLimiter({"order": 1000.0, "default": 1000.0, "ticker": 1000.0}),
        max_retries=0,
    )
    return LiveGateway(c, "KRW-BTC")


def test_mapping_done_order_with_fee_allocation() -> None:
    g = to_gateway_order(UpbitOrder.model_validate(_order()))
    assert g.state is OrderState.FILLED and g.side is Side.BID and g.client_order_id == "cid-1"
    assert (
        g.executed_volume == Decimal("0.00026")
        and g.fills[0].fee == Decimal("15.00")
        and g.paid_fee == Decimal("15")
    )
    assert g.avg_price == Decimal("29900") / Decimal("0.00026")
    assert (
        to_gateway_order(UpbitOrder.model_validate(_order(state="wait"))).state is OrderState.OPEN
    )
    assert (
        to_gateway_order(UpbitOrder.model_validate(_order(state="cancel"))).state
        is OrderState.CANCELLED
    )


@respx.mock
async def test_market_buy_sends_identifier_and_price_body() -> None:
    route = respx.post(f"{BASE}/v1/orders").mock(return_value=httpx.Response(201, json=_order()))
    g = await _gw().place_market_buy("cid-1", Decimal("30000"))
    assert g.state is OrderState.FILLED and g.exchange_uuid == "u-1"
    sent = route.calls[0].request
    assert sent.headers["Authorization"].startswith("Bearer ")
    import json

    body = json.loads(sent.content)
    assert body == {
        "market": "KRW-BTC",
        "side": "bid",
        "ord_type": "price",
        "price": "30000",
        "identifier": "cid-1",
    }


@respx.mock
async def test_market_sell_body_and_rejection_mapping() -> None:
    respx.post(f"{BASE}/v1/orders").mock(
        return_value=httpx.Response(
            400, json={"error": {"name": "insufficient_funds_ask", "message": "x"}}
        )
    )
    with pytest.raises(GatewayRejectedError) as e:
        await _gw().place_market_sell("cid-2", Decimal("0.0003"))
    assert e.value.code == "insufficient_funds_ask"


@respx.mock
async def test_network_error_and_duplicate_identifier() -> None:
    respx.post(f"{BASE}/v1/orders").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(GatewayNetworkError):
        await _gw().place_market_buy("cid-3", Decimal("30000"))
    respx.post(f"{BASE}/v1/orders").mock(
        return_value=httpx.Response(
            400, json={"error": {"name": "duplicate_identifier", "message": "x"}}
        )
    )
    with pytest.raises(DuplicateIdentifierError):
        await _gw().place_market_buy("cid-3", Decimal("30000"))


@respx.mock
async def test_get_order_by_identifier_not_found_and_cancel() -> None:
    respx.get(f"{BASE}/v1/order").mock(
        return_value=httpx.Response(
            404, json={"error": {"name": "order_not_found", "message": "no"}}
        )
    )
    with pytest.raises(OrderNotFoundError):
        await _gw().get_order(client_order_id="ghost")
    respx.get(f"{BASE}/v1/order").mock(return_value=httpx.Response(200, json=_order()))
    g = await _gw().get_order(client_order_id="cid-1")
    assert g.state is OrderState.FILLED
    respx.delete(f"{BASE}/v1/order").mock(
        return_value=httpx.Response(200, json=_order(state="cancel", executed="0"))
    )
    assert (await _gw().cancel_order(exchange_uuid="u-1")).state is OrderState.CANCELLED


@respx.mock
async def test_balances_and_open_orders() -> None:
    respx.get(f"{BASE}/v1/accounts").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "currency": "KRW",
                    "balance": "70000.5",
                    "locked": "0",
                    "avg_buy_price": "0",
                    "avg_buy_price_modified": False,
                    "unit_currency": "KRW",
                },
                {
                    "currency": "BTC",
                    "balance": "0.00026",
                    "locked": "0",
                    "avg_buy_price": "115000000",
                    "avg_buy_price_modified": False,
                    "unit_currency": "KRW",
                },
            ],
        )
    )
    b = await _gw().get_balances()
    assert (
        b.krw == Decimal("70000.5")
        and b.btc == Decimal("0.00026")
        and b.btc_avg_buy_price == Decimal("115000000")
    )
    respx.get(f"{BASE}/v1/orders/open").mock(
        return_value=httpx.Response(200, json=[_order(state="wait", identifier="cid-9")])
    )
    opens = await _gw().get_open_orders()
    assert [o.client_order_id for o in opens] == ["cid-9"] and opens[0].state is OrderState.OPEN


@respx.mock
async def test_orders_test_is_dry_run_endpoint() -> None:
    c = UpbitRestClient("AK", "SK", rate_limiter=RateLimiter({"order-test": 1000.0}))
    route = respx.post(f"{BASE}/v1/orders/test").mock(
        return_value=httpx.Response(201, json=_order(state="wait", executed="0"))
    )
    o = await c.test_order(
        market="KRW-BTC",
        side="bid",
        ord_type="price",
        price=Decimal(6000),
        identifier="preflight-1",
    )
    assert (
        o.state == "wait"
        and route.called
        and not respx.calls.last.request.url.path.endswith("/v1/orders")
    )
    await c.aclose()
