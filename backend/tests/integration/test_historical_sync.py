"""HistoricalSync against a mocked Upbit REST API (respx): pagination, resume, retry on 5xx/429."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.types import Timeframe
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.errors import UpbitHTTPError, UpbitNetworkError
from app.exchange.upbit.rate_limiter import RateLimiter
from app.exchange.upbit.rest_client import UpbitRestClient
from app.market.historical import HistoricalSync, validate_1m

END = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
TOTAL_MINUTES = 700  # -> 4 pages of 200


def _candle_json(ts: datetime, i: int) -> dict[str, Any]:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "candle_date_time_kst": (ts + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S"),
        "opening_price": 100.0 + i,
        "high_price": 101.0 + i,
        "low_price": 99.0 + i,
        "trade_price": 100.5 + i,
        "timestamp": int(ts.timestamp() * 1000),
        "candle_acc_trade_price": 1000.0,
        "candle_acc_trade_volume": 1.0,
        "unit": 1,
    }


ALL = [END - timedelta(minutes=TOTAL_MINUTES - i) for i in range(TOTAL_MINUTES)]  # ascending
SKIP = {ALL[100], ALL[101]}  # simulate two no-trade minutes (Upbit emits no candle)


def _handler(request: httpx.Request) -> httpx.Response:
    params = dict(request.url.params)
    count = int(params.get("count", 1))
    to = (
        datetime.strptime(params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if "to" in params
        else END + timedelta(minutes=1)
    )
    eligible = [t for t in ALL if t < to and t not in SKIP]
    page = eligible[-count:]
    page.reverse()  # Upbit returns newest first
    return httpx.Response(
        200,
        json=[_candle_json(t, ALL.index(t)) for t in page],
        headers={"Remaining-Req": "group=candles; min=600; sec=9"},
    )


@pytest.fixture
async def repo(tmp_path: Any) -> Repository:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await init_schema(engine)
    return Repository(engine)


def _fast_client() -> UpbitRestClient:
    return UpbitRestClient(rate_limiter=RateLimiter({"candle": 10000.0}), max_retries=3)


@respx.mock
async def test_sync_paginates_and_is_resumable(repo: Repository) -> None:
    route = respx.get("https://api.upbit.com/v1/candles/minutes/1").mock(side_effect=_handler)
    start = ALL[0]
    async with _fast_client() as client:
        rep = await HistoricalSync(client, repo).sync("KRW-BTC", start, END)
    assert rep.db_count == TOTAL_MINUTES - len(SKIP)
    assert rep.requests == 4
    first_calls = route.call_count

    # re-run: nothing new -> zero or minimal requests, no duplicates
    async with _fast_client() as client:
        rep2 = await HistoricalSync(client, repo).sync("KRW-BTC", start, END)
    assert rep2.db_count == rep.db_count
    assert route.call_count - first_calls <= 1

    # extend window older by 60 minutes -> only the head is fetched
    older = start - timedelta(minutes=60)
    ALL[:0] = [older + timedelta(minutes=i) for i in range(60)]
    try:
        async with _fast_client() as client:
            rep3 = await HistoricalSync(client, repo).sync("KRW-BTC", older, END)
        assert rep3.db_count == rep.db_count + 60
        assert rep3.requests == 1
    finally:
        del ALL[:60]

    candles = await repo.load_candles("KRW-BTC", Timeframe.M1)
    v = validate_1m(candles)
    assert v.duplicates == 0 and v.out_of_order == 0 and v.ohlc_violations == 0
    assert v.missing_minutes == 2 and len(v.gaps) == 1 and v.gaps[0][2] == 2


@respx.mock
async def test_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="upstream")
        return _handler(request)

    respx.get("https://api.upbit.com/v1/candles/minutes/1").mock(side_effect=flaky)
    async with _fast_client() as client:
        out = await client.get_minute_candles("KRW-BTC", 1, count=5)
    assert len(out) == 5 and calls["n"] == 3
    assert client.metrics.errors == 2 and client.metrics.consecutive_errors == 0


@respx.mock
async def test_429_then_success_and_gives_up_after_retries() -> None:
    respx.get("https://api.upbit.com/v1/ticker").mock(
        side_effect=[
            httpx.Response(429, headers={"Remaining-Req": "group=ticker; min=600; sec=0"}),
            httpx.Response(
                200,
                json=[
                    {
                        "market": "KRW-BTC",
                        "trade_price": 1.0,
                        "trade_timestamp": 1,
                        "timestamp": 1,
                        "acc_trade_volume_24h": 1.0,
                        "change": "RISE",
                    }
                ],
            ),
        ]
    )
    async with UpbitRestClient(
        rate_limiter=RateLimiter({"ticker": 10000.0}), max_retries=1
    ) as client:
        t = await client.get_ticker("KRW-BTC")
        assert t.trade_price == 1 and client.metrics.rate_limited == 1

    respx.get("https://api.upbit.com/v1/candles/minutes/1").mock(
        return_value=httpx.Response(500, text="x")
    )
    async with UpbitRestClient(
        rate_limiter=RateLimiter({"candle": 10000.0}), max_retries=1
    ) as client:
        with pytest.raises(UpbitHTTPError):
            await client.get_minute_candles("KRW-BTC", 1)


@respx.mock
async def test_network_error_surfaces_after_retries() -> None:
    respx.get("https://api.upbit.com/v1/ticker").mock(side_effect=httpx.ConnectError("boom"))
    async with UpbitRestClient(
        rate_limiter=RateLimiter({"ticker": 10000.0}), max_retries=0
    ) as client:
        with pytest.raises(UpbitNetworkError):
            await client.get_ticker("KRW-BTC")


@respx.mock
async def test_error_body_is_parsed() -> None:
    respx.get("https://api.upbit.com/v1/candles/minutes/1").mock(
        return_value=httpx.Response(
            400, json={"error": {"name": "invalid_query_payload", "message": "bad"}}
        )
    )
    async with UpbitRestClient(rate_limiter=RateLimiter({"candle": 10000.0})) as client:
        with pytest.raises(UpbitHTTPError) as ei:
            await client.get_minute_candles("KRW-BTC", 1)
    assert ei.value.name == "invalid_query_payload" and ei.value.status == 400
