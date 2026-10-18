"""Exchange gateway interface shared by PAPER (simulated) and LIVE (Upbit) implementations.

Strategy and RiskManager never see this; only OrderManager / Reconciler / RecoveryChecker do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.core.types import Fill, OrderState, OrdType, Side


class OrderNotFoundError(Exception):
    pass


class DuplicateIdentifierError(Exception):
    """Exchange already has an order with this identifier (idempotency key)."""


class GatewayRejectedError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class GatewayNetworkError(Exception):
    """Request may or may not have reached the exchange -> order state UNKNOWN."""


@dataclass(slots=True)
class GatewayOrder:
    client_order_id: str
    exchange_uuid: str | None
    market: str
    side: Side
    ord_type: OrdType
    state: OrderState
    requested_krw: Decimal | None
    requested_volume: Decimal | None
    executed_volume: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    paid_fee: Decimal = Decimal(0)
    fills: list[Fill] = field(default_factory=list)
    created_at: datetime | None = None
    error_code: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


@dataclass(frozen=True, slots=True)
class Balances:
    krw: Decimal
    krw_locked: Decimal
    btc: Decimal
    btc_locked: Decimal
    btc_avg_buy_price: Decimal | None = None
    others: dict[str, Decimal] = field(default_factory=dict)  # other coins (total qty), e.g. ETH


class ExchangeGateway(Protocol):
    name: str

    async def place_market_buy(self, client_order_id: str, krw_amount: Decimal) -> GatewayOrder: ...

    async def place_market_sell(self, client_order_id: str, volume: Decimal) -> GatewayOrder: ...

    async def get_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder: ...

    async def cancel_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder: ...

    async def get_open_orders(self) -> list[GatewayOrder]: ...

    async def get_balances(self) -> Balances: ...

    async def get_reference_price(self) -> Decimal: ...

    async def ping(self) -> bool: ...
