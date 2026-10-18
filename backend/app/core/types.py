"""Shared domain types. Money and quantities are always Decimal; floats only inside indicators."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any


class TradingMode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class BotStatus(StrEnum):
    STARTING = "STARTING"
    ONLINE = "ONLINE"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    STOPPED = "STOPPED"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Side(StrEnum):
    """Upbit side codes: bid = buy, ask = sell."""

    BID = "bid"
    ASK = "ask"


class OrdType(StrEnum):
    """Upbit ord_type codes."""

    LIMIT = "limit"
    PRICE = "price"  # market BUY (price = total KRW)
    MARKET = "market"  # market SELL (volume = quantity)
    BEST = "best"


class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H2 = "2h"  # research-only (Round 3 shadow); not a valid Upbit REST/WS candle unit
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}[self.value]

    @property
    def delta(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    @property
    def upbit_unit(self) -> int:
        """Unit for GET /v1/candles/minutes/{unit} and WebSocket candle.{unit}m."""
        return self.minutes


@dataclass(frozen=True, slots=True)
class Candle:
    market: str
    timeframe: Timeframe
    open_time: datetime  # UTC, tz-aware, bar start
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # base asset (BTC)
    quote_volume: Decimal  # KRW traded
    is_closed: bool = True

    @property
    def close_time(self) -> datetime:
        return self.open_time + self.timeframe.delta


@dataclass(frozen=True, slots=True)
class Signal:
    action: Action
    strength: float  # 0.0 ~ 1.0
    reason: str
    timestamp: datetime  # UTC — the bar close time the signal is based on
    strategy: str
    timeframe: Timeframe
    indicators: dict[str, float] = field(default_factory=dict)
    stop_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str | None = None  # populated when rejected (e.g. DAILY_LOSS_LIMIT)
    order_krw: Decimal | None = None  # approved BUY amount
    order_volume: Decimal | None = None  # approved SELL quantity
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str  # -> Upbit `identifier`, unique per account, idempotency key
    market: str
    side: Side
    ord_type: OrdType
    krw_amount: Decimal | None  # for PRICE (market buy)
    volume: Decimal | None  # for MARKET (market sell)
    signal_id: int | None
    mode: TradingMode


@dataclass(frozen=True, slots=True)
class Fill:
    order_client_id: str
    price: Decimal
    volume: Decimal
    funds: Decimal  # price * volume in KRW
    fee: Decimal
    timestamp: datetime
    exchange_trade_uuid: str | None = None


@dataclass(slots=True)
class OrderRecord:
    """Persisted view of an order (mirrors OrderRow). Owned by OrderManager."""

    client_order_id: str
    mode: TradingMode
    market: str
    side: Side
    ord_type: OrdType
    state: OrderState
    created_at: datetime
    updated_at: datetime
    req_krw: Decimal | None = None
    req_volume: Decimal | None = None
    exchange_uuid: str | None = None
    executed_volume: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    paid_fee: Decimal = Decimal(0)
    signal_id: int | None = None
    error_code: str | None = None
    submitted_at: datetime | None = None
    fills: list[Fill] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    db_id: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)

    @property
    def is_inflight(self) -> bool:
        return self.state in (
            OrderState.SUBMITTED,
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.UNKNOWN,
        )
