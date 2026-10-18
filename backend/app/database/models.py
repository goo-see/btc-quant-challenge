"""SQLAlchemy ORM tables. All datetimes are UTC (tz-aware in Python, stored as ISO/naive-UTC).

Types restricted to what both SQLite and PostgreSQL support so migration is a URL change.
Money uses Numeric(24, 8) -> Decimal.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Money = Numeric(24, 8)


class Base(DeclarativeBase):
    type_annotation_map = {
        Decimal: Money,
        dict[str, Any]: JSON,
        datetime: DateTime(timezone=True),
    }


class CandleRow(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("market", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candles_lookup", "market", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(5))
    open_time: Mapped[datetime]
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    quote_volume: Mapped[Decimal]
    source: Mapped[str] = mapped_column(String(10), default="rest")  # rest | ws | resample


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column(String(10))
    run_id: Mapped[str] = mapped_column(String(40))
    strategy: Mapped[str] = mapped_column(String(50))
    timeframe: Mapped[str] = mapped_column(String(5))
    action: Mapped[str] = mapped_column(String(5))
    strength: Mapped[float]
    reason: Mapped[str] = mapped_column(Text)
    indicators: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stop_price: Mapped[Decimal | None]


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    ts: Mapped[datetime]
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(50))
    order_krw: Mapped[Decimal | None]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)  # Upbit identifier
    exchange_uuid: Mapped[str | None] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(10))
    market: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(3))
    ord_type: Mapped[str] = mapped_column(String(10))
    req_price: Mapped[Decimal | None]
    req_volume: Mapped[Decimal | None]
    req_krw: Mapped[Decimal | None]
    state: Mapped[str] = mapped_column(String(20), index=True)
    executed_volume: Mapped[Decimal] = mapped_column(default=Decimal(0))
    avg_fill_price: Mapped[Decimal | None]
    paid_fee: Mapped[Decimal] = mapped_column(default=Decimal(0))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]
    submitted_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    exchange_trade_uuid: Mapped[str | None] = mapped_column(String(64), unique=True)
    price: Mapped[Decimal]
    volume: Mapped[Decimal]
    funds: Mapped[Decimal]
    fee: Mapped[Decimal]
    ts: Mapped[datetime]


class TradeRow(Base):
    """Round-trip trade (entry -> exit). Basis for win rate / PF / expectancy."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    entry_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    entry_time: Mapped[datetime]
    entry_price: Mapped[Decimal]
    exit_time: Mapped[datetime | None]
    exit_price: Mapped[Decimal | None]
    quantity: Mapped[Decimal]
    gross_pnl: Mapped[Decimal | None]
    fees: Mapped[Decimal] = mapped_column(default=Decimal(0))
    net_pnl: Mapped[Decimal | None]
    return_pct: Mapped[Decimal | None]
    holding_sec: Mapped[int | None]
    strategy: Mapped[str] = mapped_column(String(50))
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN | CLOSED


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column(String(10))
    krw: Mapped[Decimal]
    btc_qty: Mapped[Decimal]
    btc_price: Mapped[Decimal]
    btc_value: Mapped[Decimal]
    equity: Mapped[Decimal]
    avg_entry: Mapped[Decimal | None]
    realized_pnl: Mapped[Decimal]
    unrealized_pnl: Mapped[Decimal]
    fees_total: Mapped[Decimal]
    hwm: Mapped[Decimal]
    drawdown: Mapped[Decimal]


class DailyStatsRow(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("date", "mode", name="uq_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in report timezone (KST)
    mode: Mapped[str] = mapped_column(String(10))
    start_equity: Mapped[Decimal]
    end_equity: Mapped[Decimal]
    daily_pnl: Mapped[Decimal]
    trades: Mapped[int] = mapped_column(default=0)
    wins: Mapped[int] = mapped_column(default=0)
    losses: Mapped[int] = mapped_column(default=0)
    fees: Mapped[Decimal] = mapped_column(default=Decimal(0))
    best_trade: Mapped[Decimal | None]
    worst_trade: Mapped[Decimal | None]
    max_dd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    errors: Mapped[int] = mapped_column(default=0)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    level: Mapped[str] = mapped_column(String(10))
    type: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReconciliationRow(Base):
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    internal_krw: Mapped[Decimal]
    exchange_krw: Mapped[Decimal]
    internal_btc: Mapped[Decimal]
    exchange_btc: Mapped[Decimal]
    krw_diff: Mapped[Decimal]
    btc_diff: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(10))  # OK | WARN | FAIL


class BotStateRow(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime]


class CommandRow(Base):
    """Operator command (Dashboard/API -> Trader). Lifecycle:
    PENDING -> ACKNOWLEDGED -> EXECUTED | FAILED ; PENDING -> EXPIRED (TTL) | REJECTED."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True)  # client-generated, idempotent
    ts: Mapped[datetime]  # requested_at
    type: Mapped[str] = mapped_column(String(10))  # PAUSE | RESUME | KILL
    requested_by: Mapped[str] = mapped_column(String(120))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    expires_at: Mapped[datetime | None]
    acknowledged_at: Mapped[datetime | None]
    executed_at: Mapped[datetime | None]
    processed_at: Mapped[datetime | None]
    failure_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    audit: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # ip, tailscale identity, ua


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime]
    strategy: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timeframe: Mapped[str] = mapped_column(String(5))
    start: Mapped[datetime]
    end: Mapped[datetime]
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    equity_curve_path: Mapped[str | None] = mapped_column(String(255))
    is_walk_forward: Mapped[bool] = mapped_column(Boolean, default=False)


class ExecutionMetricRow(Base):
    """Per-fill execution quality (slippage vs signal, stop distances, exit economics)."""

    __tablename__ = "execution_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    strategy_version: Mapped[str] = mapped_column(String(60))
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    signal_time: Mapped[datetime | None]
    signal_price: Mapped[Decimal | None]
    order_sent_at: Mapped[datetime | None]
    reference_price_at_send: Mapped[Decimal | None]
    actual_fill_price: Mapped[Decimal]  # VWAP over fills
    slippage_bps: Mapped[Decimal | None]  # vs signal price (adverse = positive)
    slippage_vs_reference_bps: Mapped[Decimal | None]  # vs reference price at send
    fee: Mapped[Decimal] = mapped_column(default=Decimal(0))
    quantity: Mapped[Decimal]
    funds: Mapped[Decimal]
    fill_count: Mapped[int] = mapped_column(default=1)
    atr: Mapped[Decimal | None]
    initial_stop: Mapped[Decimal | None]
    stop_distance_from_signal: Mapped[Decimal | None]
    stop_distance_from_fill: Mapped[Decimal | None]
    exit_reason: Mapped[str | None] = mapped_column(Text)
    exit_trigger_price: Mapped[Decimal | None]
    realized_pnl: Mapped[Decimal | None]
    return_pct: Mapped[Decimal | None]
    holding_sec: Mapped[int | None]
    entry_time: Mapped[datetime | None]
    filled_at: Mapped[datetime]
    created_at: Mapped[datetime]
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class HealthSnapshotRow(Base):
    __tablename__ = "health_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    ws_connected: Mapped[bool] = mapped_column(Boolean)
    reconnect_count: Mapped[int]
    msg_count: Mapped[int]
    last_data_ts: Mapped[datetime | None]
    rest_latency_ms: Mapped[float]
    rest_errors: Mapped[int]
    db_ok: Mapped[bool] = mapped_column(Boolean)
    heartbeat_ts: Mapped[datetime]
    uptime_sec: Mapped[int]
