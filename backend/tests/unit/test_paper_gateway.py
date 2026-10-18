from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.types import OrderState, Side
from app.exchange.base import (
    DuplicateIdentifierError,
    GatewayNetworkError,
    GatewayRejectedError,
    OrderNotFoundError,
)
from app.execution.fill_model import FillParams
from app.execution.paper_gateway import PaperGateway

P = FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(3))


def _gw() -> PaperGateway:
    gw = PaperGateway(market="KRW-BTC", initial_krw=Decimal(100000), fill_params=P)
    gw.update_price(Decimal(100_000_000))
    return gw


async def test_buy_then_sell_ledger_and_balances() -> None:
    gw = _gw()
    o = await gw.place_market_buy("c1", Decimal(30000))
    assert o.state is OrderState.FILLED and o.side is Side.BID and len(o.fills) == 1
    bal = await gw.get_balances()
    assert bal.krw == gw.ledger.krw and bal.btc == o.executed_volume > 0
    assert bal.krw + o.fills[0].funds + o.fills[0].fee == Decimal(100000)
    s = await gw.place_market_sell("c2", bal.btc)
    assert s.state is OrderState.FILLED
    bal2 = await gw.get_balances()
    assert bal2.btc == 0 and bal2.btc_avg_buy_price is None
    assert bal2.krw < Decimal(100000)  # fees + slippage
    assert await gw.get_open_orders() == []


async def test_rejections_like_upbit() -> None:
    gw = _gw()
    with pytest.raises(GatewayRejectedError) as e:
        await gw.place_market_buy("c1", Decimal(4999))
    assert e.value.code == "under_min_total_bid"
    with pytest.raises(GatewayRejectedError) as e:
        await gw.place_market_buy("c2", Decimal(100000))  # no room for fee
    assert e.value.code == "insufficient_funds_bid"
    with pytest.raises(GatewayRejectedError) as e:
        await gw.place_market_sell("c3", Decimal("0.001"))
    assert e.value.code == "insufficient_funds_ask"
    with pytest.raises(GatewayRejectedError) as e:
        await gw.place_market_sell("c4", Decimal("0.00001"))
    assert e.value.code == "under_min_total_ask"


async def test_duplicate_identifier_and_lookup() -> None:
    gw = _gw()
    await gw.place_market_buy("dup", Decimal(10000))
    with pytest.raises(DuplicateIdentifierError):
        await gw.place_market_buy("dup", Decimal(10000))
    o = await gw.get_order(client_order_id="dup")
    assert o.state is OrderState.FILLED
    with pytest.raises(OrderNotFoundError):
        await gw.get_order(client_order_id="nope")


async def test_fault_injection_network_after_accept_still_records_order() -> None:
    gw = _gw()
    gw.faults.next_place = "network_after_accept"
    with pytest.raises(GatewayNetworkError):
        await gw.place_market_buy("n1", Decimal(10000))
    o = await gw.get_order(client_order_id="n1")  # exchange DID execute it
    assert o.state is OrderState.FILLED and gw.ledger.btc > 0


async def test_fault_injection_network_before_accept_leaves_nothing() -> None:
    gw = _gw()
    gw.faults.next_place = "network_before_accept"
    with pytest.raises(GatewayNetworkError):
        await gw.place_market_buy("n2", Decimal(10000))
    with pytest.raises(OrderNotFoundError):
        await gw.get_order(client_order_id="n2")


async def test_partial_sell_ends_cancelled_with_executed_volume() -> None:
    gw = _gw()
    await gw.place_market_buy("b", Decimal(30000))
    vol = gw.ledger.btc
    gw.faults.next_place = "partial"
    o = await gw.place_market_sell("s", vol)
    assert o.state is OrderState.CANCELLED and 0 < o.executed_volume < vol
    assert gw.ledger.btc == vol - o.executed_volume


async def test_ledger_roundtrip_json() -> None:
    gw = _gw()
    await gw.place_market_buy("b", Decimal(30000))
    d = gw.to_json()
    gw2 = PaperGateway(market="KRW-BTC", initial_krw=Decimal(1), fill_params=P)
    gw2.restore(d)
    assert gw2.ledger == gw.ledger
