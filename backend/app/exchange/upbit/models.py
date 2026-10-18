"""Pydantic models for Upbit responses (field names verified against docs 2026-09-24)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class UpbitCandle(BaseModel):
    """GET /v1/candles/minutes/{unit}."""

    model_config = ConfigDict(extra="ignore")

    market: str
    candle_date_time_utc: str
    candle_date_time_kst: str
    opening_price: Decimal
    high_price: Decimal
    low_price: Decimal
    trade_price: Decimal
    timestamp: int | None = None  # null on very old candles (observed 2019-03 and earlier)
    candle_acc_trade_price: Decimal
    candle_acc_trade_volume: Decimal
    unit: int

    @property
    def open_time(self) -> datetime:
        return datetime.fromisoformat(self.candle_date_time_utc).replace(tzinfo=UTC)


class UpbitTicker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    trade_price: Decimal
    trade_timestamp: int
    timestamp: int
    acc_trade_volume_24h: Decimal
    change: str


class UpbitAccount(BaseModel):
    """GET /v1/accounts."""

    model_config = ConfigDict(extra="ignore")

    currency: str
    balance: Decimal
    locked: Decimal
    avg_buy_price: Decimal
    avg_buy_price_modified: bool
    unit_currency: str


class UpbitMarketSide(BaseModel):
    model_config = ConfigDict(extra="ignore")
    currency: str
    min_total: Decimal


class UpbitMarketInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    state: str
    bid: UpbitMarketSide
    ask: UpbitMarketSide
    max_total: Decimal


class UpbitOrderChance(BaseModel):
    """GET /v1/orders/chance."""

    model_config = ConfigDict(extra="ignore")

    bid_fee: Decimal
    ask_fee: Decimal
    maker_bid_fee: Decimal | None = None
    maker_ask_fee: Decimal | None = None
    market: UpbitMarketInfo
    bid_account: UpbitAccount
    ask_account: UpbitAccount


class UpbitTrade(BaseModel):
    model_config = ConfigDict(extra="ignore")
    market: str
    uuid: str
    price: Decimal
    volume: Decimal
    funds: Decimal
    side: str
    created_at: str


class UpbitOrder(BaseModel):
    """POST /v1/orders, POST /v1/orders/test, GET /v1/order, DELETE /v1/order."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    side: str
    ord_type: str
    price: Decimal | None = None
    state: str  # wait | watch | done | cancel
    market: str
    created_at: str
    volume: Decimal | None = None
    remaining_volume: Decimal | None = None
    reserved_fee: Decimal = Decimal(0)
    remaining_fee: Decimal = Decimal(0)
    paid_fee: Decimal = Decimal(0)
    locked: Decimal = Decimal(0)
    executed_volume: Decimal = Decimal(0)
    trades_count: int = 0
    identifier: str | None = None
    time_in_force: str | None = None
    trades: list[UpbitTrade] = Field(default_factory=list)
