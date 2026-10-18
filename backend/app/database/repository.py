"""Repository layer: the only place SQL lives. Swap engine URL to move to PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.types import (
    Candle,
    Fill,
    OrderRecord,
    OrderState,
    OrdType,
    RiskDecision,
    Side,
    Signal,
    Timeframe,
    TradingMode,
)
from app.database.engine import session_factory, session_scope, with_lock_retry
from app.database.models import (
    BacktestRunRow,
    BotStateRow,
    CandleRow,
    CommandRow,
    EventRow,
    ExecutionMetricRow,
    FillRow,
    HealthSnapshotRow,
    OrderRow,
    PortfolioSnapshotRow,
    ReconciliationRow,
    RiskDecisionRow,
    SignalRow,
    TradeRow,
)


def _utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; normalise to tz-aware UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _jsonable(v)
        else:
            out[k] = v
    return out


class Repository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._factory: async_sessionmaker[AsyncSession] = session_factory(engine)
        self.write_errors = 0

    async def _write[T](self, fn: Any) -> T:
        try:
            result: T = await with_lock_retry(fn)
            return result
        except Exception:
            self.write_errors += 1
            raise

    # ---------------------------------------------------------------- candles
    async def upsert_candles(self, candles: Sequence[Candle], source: str = "rest") -> int:
        """Insert-or-ignore on (market, timeframe, open_time). Returns rows attempted."""
        if not candles:
            return 0
        rows = [
            {
                "market": c.market,
                "timeframe": c.timeframe.value,
                "open_time": c.open_time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "quote_volume": c.quote_volume,
                "source": source,
            }
            for c in candles
        ]

        async def _do() -> int:
            async with session_scope(self._factory) as s:
                if self._engine.dialect.name == "sqlite":
                    stmt = (
                        sqlite_insert(CandleRow)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["market", "timeframe", "open_time"])
                    )
                    await s.execute(stmt)
                else:
                    for r in rows:
                        s.add(CandleRow(**r))
            return len(rows)

        return await self._write(_do)

    async def candle_range(
        self, market: str, timeframe: Timeframe
    ) -> tuple[datetime | None, datetime | None, int]:
        async with self._factory() as s:
            res = await s.execute(
                select(
                    func.min(CandleRow.open_time),
                    func.max(CandleRow.open_time),
                    func.count(CandleRow.id),
                ).where(CandleRow.market == market, CandleRow.timeframe == timeframe.value)
            )
            mn, mx, cnt = res.one()
            return (_utc(mn) if mn else None, _utc(mx) if mx else None, int(cnt))

    async def load_candles(
        self,
        market: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        stmt = select(CandleRow).where(
            CandleRow.market == market, CandleRow.timeframe == timeframe.value
        )
        if start is not None:
            stmt = stmt.where(CandleRow.open_time >= start)
        if end is not None:
            stmt = stmt.where(CandleRow.open_time < end)
        stmt = stmt.order_by(CandleRow.open_time)
        async with self._factory() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [
            Candle(
                market=r.market,
                timeframe=Timeframe(r.timeframe),
                open_time=_utc(r.open_time),
                open=_dec(r.open),
                high=_dec(r.high),
                low=_dec(r.low),
                close=_dec(r.close),
                volume=_dec(r.volume),
                quote_volume=_dec(r.quote_volume),
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- events
    async def add_event(
        self, level: str, type_: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                s.add(
                    EventRow(
                        ts=datetime.now(UTC),
                        level=level,
                        type=type_,
                        message=message,
                        payload=_jsonable(payload or {}),
                    )
                )

        await self._write(_do)

    async def count_events(self, *, level: str, since: datetime) -> int:
        async with self._factory() as s:
            n = await s.scalar(
                select(func.count(EventRow.id)).where(EventRow.level == level, EventRow.ts >= since)
            )
            return int(n or 0)

    async def recent_events(self, limit: int = 50) -> list[EventRow]:
        async with self._factory() as s:
            rows = (
                await s.execute(select(EventRow).order_by(EventRow.id.desc()).limit(limit))
            ).scalars()
            return list(rows)

    # ------------------------------------------------------------ signals/risk
    async def save_signal(self, sig: Signal, mode: TradingMode, run_id: str) -> int:
        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = SignalRow(
                    ts=sig.timestamp,
                    mode=mode.value,
                    run_id=run_id,
                    strategy=sig.strategy,
                    timeframe=sig.timeframe.value,
                    action=sig.action.value,
                    strength=sig.strength,
                    reason=sig.reason,
                    indicators={k: float(v) for k, v in sig.indicators.items()},
                    stop_price=sig.stop_price,
                )
                s.add(row)
                await s.flush()
                return int(row.id)

        return await self._write(_do)

    async def save_risk_decision(self, signal_id: int | None, d: RiskDecision) -> int:
        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = RiskDecisionRow(
                    signal_id=signal_id,
                    ts=datetime.now(UTC),
                    approved=d.approved,
                    reason=d.reason,
                    order_krw=d.order_krw,
                    snapshot=_jsonable(d.snapshot),
                )
                s.add(row)
                await s.flush()
                return int(row.id)

        return await self._write(_do)

    async def recent_signals(self, limit: int = 20) -> list[SignalRow]:
        async with self._factory() as s:
            rows = (
                await s.execute(select(SignalRow).order_by(SignalRow.id.desc()).limit(limit))
            ).scalars()
            return list(rows)

    # ----------------------------------------------------------------- orders
    @staticmethod
    def _row_to_order(r: OrderRow, fills: list[FillRow]) -> OrderRecord:
        return OrderRecord(
            client_order_id=r.client_order_id,
            mode=TradingMode(r.mode),
            market=r.market,
            side=Side(r.side),
            ord_type=OrdType(r.ord_type),
            state=OrderState(r.state),
            created_at=_utc(r.created_at),
            updated_at=_utc(r.updated_at),
            req_krw=_dec(r.req_krw) if r.req_krw is not None else None,
            req_volume=_dec(r.req_volume) if r.req_volume is not None else None,
            exchange_uuid=r.exchange_uuid,
            executed_volume=_dec(r.executed_volume),
            avg_fill_price=_dec(r.avg_fill_price) if r.avg_fill_price is not None else None,
            paid_fee=_dec(r.paid_fee),
            signal_id=r.signal_id,
            error_code=r.error_code,
            submitted_at=_utc(r.submitted_at) if r.submitted_at else None,
            fills=[
                Fill(
                    order_client_id=r.client_order_id,
                    price=_dec(f.price),
                    volume=_dec(f.volume),
                    funds=_dec(f.funds),
                    fee=_dec(f.fee),
                    timestamp=_utc(f.ts),
                    exchange_trade_uuid=f.exchange_trade_uuid,
                )
                for f in fills
            ],
            raw=dict(r.raw or {}),
            db_id=r.id,
        )

    async def save_order(self, o: OrderRecord) -> int:
        """Insert or update by client_order_id; fills are appended idempotently by trade uuid."""

        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = (
                    await s.execute(
                        select(OrderRow).where(OrderRow.client_order_id == o.client_order_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = OrderRow(client_order_id=o.client_order_id, created_at=o.created_at)
                    s.add(row)
                row.exchange_uuid = o.exchange_uuid
                row.mode = o.mode.value
                row.market = o.market
                row.side = o.side.value
                row.ord_type = o.ord_type.value
                row.req_krw = o.req_krw
                row.req_volume = o.req_volume
                row.state = o.state.value
                row.executed_volume = o.executed_volume
                row.avg_fill_price = o.avg_fill_price
                row.paid_fee = o.paid_fee
                row.signal_id = o.signal_id
                row.error_code = o.error_code
                row.raw = _jsonable(o.raw)
                row.submitted_at = o.submitted_at
                row.updated_at = o.updated_at
                await s.flush()
                existing = {
                    f.exchange_trade_uuid
                    for f in (
                        await s.execute(select(FillRow).where(FillRow.order_id == row.id))
                    ).scalars()
                }
                for f in o.fills:
                    key = (
                        f.exchange_trade_uuid
                        or f"{o.client_order_id}:{f.timestamp.isoformat()}:{f.volume}"
                    )
                    if key in existing:
                        continue
                    existing.add(key)
                    s.add(
                        FillRow(
                            order_id=row.id,
                            exchange_trade_uuid=key,
                            price=f.price,
                            volume=f.volume,
                            funds=f.funds,
                            fee=f.fee,
                            ts=f.timestamp,
                        )
                    )
                return int(row.id)

        oid: int = await self._write(_do)
        o.db_id = oid
        return oid

    async def get_order(self, client_order_id: str) -> OrderRecord | None:
        async with self._factory() as s:
            row = (
                await s.execute(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            fills = list(
                (await s.execute(select(FillRow).where(FillRow.order_id == row.id))).scalars()
            )
            return self._row_to_order(row, fills)

    async def inflight_orders(self, mode: TradingMode) -> list[OrderRecord]:
        states = [
            OrderState.SUBMITTED.value,
            OrderState.OPEN.value,
            OrderState.PARTIALLY_FILLED.value,
            OrderState.UNKNOWN.value,
        ]
        async with self._factory() as s:
            rows = list(
                (
                    await s.execute(
                        select(OrderRow).where(
                            OrderRow.mode == mode.value, OrderRow.state.in_(states)
                        )
                    )
                ).scalars()
            )
            return [self._row_to_order(r, []) for r in rows]

    async def count_orders_since(self, mode: TradingMode, since: datetime) -> int:
        async with self._factory() as s:
            n = await s.scalar(
                select(func.count(OrderRow.id)).where(
                    OrderRow.mode == mode.value,
                    OrderRow.created_at >= since,
                    OrderRow.state != OrderState.REJECTED.value,
                )
            )
            return int(n or 0)

    async def recent_orders(
        self, mode: TradingMode, limit: int = 50, *, market: str | None = None
    ) -> list[OrderRecord]:
        stmt = select(OrderRow).where(OrderRow.mode == mode.value)
        if market is not None:
            stmt = stmt.where(OrderRow.market == market)
        stmt = stmt.order_by(OrderRow.id.desc()).limit(limit)
        async with self._factory() as s:
            rows = list((await s.execute(stmt)).scalars())
            return [self._row_to_order(r, []) for r in rows]

    async def recent_orders_with_fills(
        self, mode: TradingMode, limit: int = 50, *, market: str | None = None
    ) -> list[OrderRecord]:
        """Like recent_orders but with the fill rows attached (dashboards, fill timelines)."""
        stmt = select(OrderRow).where(OrderRow.mode == mode.value)
        if market is not None:
            stmt = stmt.where(OrderRow.market == market)
        stmt = stmt.order_by(OrderRow.id.desc()).limit(limit)
        async with self._factory() as s:
            rows = list((await s.execute(stmt)).scalars())
            out: list[OrderRecord] = []
            for r in rows:
                fills = list(
                    (await s.execute(select(FillRow).where(FillRow.order_id == r.id))).scalars()
                )
                out.append(self._row_to_order(r, fills))
            return out

    async def count_orders_between(
        self, mode: TradingMode, since: datetime, *, market: str | None = None
    ) -> int:
        stmt = select(func.count(OrderRow.id)).where(
            OrderRow.mode == mode.value,
            OrderRow.created_at >= since,
            OrderRow.state != OrderState.REJECTED.value,
        )
        if market is not None:
            stmt = stmt.where(OrderRow.market == market)
        async with self._factory() as s:
            n = await s.scalar(stmt)
            return int(n or 0)

    # ----------------------------------------------------------------- trades
    async def open_trade(
        self,
        *,
        mode: TradingMode,
        entry_order_id: int | None,
        entry_time: datetime,
        entry_price: Decimal,
        quantity: Decimal,
        fees: Decimal,
        strategy: str,
        entry_reason: str,
    ) -> int:
        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = TradeRow(
                    mode=mode.value,
                    entry_order_id=entry_order_id,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    quantity=quantity,
                    fees=fees,
                    strategy=strategy,
                    entry_reason=entry_reason,
                    status="OPEN",
                )
                s.add(row)
                await s.flush()
                return int(row.id)

        return await self._write(_do)

    async def close_trade(
        self,
        trade_id: int,
        *,
        exit_order_id: int | None,
        exit_time: datetime,
        exit_price: Decimal,
        gross_pnl: Decimal,
        fees_total: Decimal,
        net_pnl: Decimal,
        return_pct: Decimal,
        exit_reason: str,
    ) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                row = await s.get(TradeRow, trade_id)
                if row is None:
                    raise KeyError(f"trade {trade_id} not found")
                row.exit_order_id = exit_order_id
                row.exit_time = exit_time
                row.exit_price = exit_price
                row.gross_pnl = gross_pnl
                row.fees = fees_total
                row.net_pnl = net_pnl
                row.return_pct = return_pct
                row.holding_sec = int((exit_time - _utc(row.entry_time)).total_seconds())
                row.exit_reason = exit_reason
                row.status = "CLOSED"

        await self._write(_do)

    async def get_open_trade(self, mode: TradingMode) -> TradeRow | None:
        async with self._factory() as s:
            return (
                await s.execute(
                    select(TradeRow).where(TradeRow.mode == mode.value, TradeRow.status == "OPEN")
                )
            ).scalar_one_or_none()

    # ---------------------------------------------------------- execution metrics
    async def add_execution_metric(self, values: dict[str, Any]) -> int:
        """Idempotent on client_order_id (a re-applied fill never duplicates the row)."""

        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = (
                    await s.execute(
                        select(ExecutionMetricRow).where(
                            ExecutionMetricRow.client_order_id == values["client_order_id"]
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = ExecutionMetricRow(**values)
                    s.add(row)
                    await s.flush()
                return int(row.id)

        oid: int = await self._write(_do)
        return oid

    async def recent_execution_metrics(
        self, mode: TradingMode, limit: int = 50
    ) -> list[ExecutionMetricRow]:
        async with self._factory() as s:
            return list(
                (
                    await s.execute(
                        select(ExecutionMetricRow)
                        .where(ExecutionMetricRow.mode == mode.value)
                        .order_by(ExecutionMetricRow.id.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def closed_trades(
        self, mode: TradingMode, since: datetime | None = None, limit: int | None = None
    ) -> list[TradeRow]:
        stmt = select(TradeRow).where(TradeRow.mode == mode.value, TradeRow.status == "CLOSED")
        if since is not None:
            stmt = stmt.where(TradeRow.exit_time >= since)
        stmt = stmt.order_by(TradeRow.exit_time.desc())
        if limit:
            stmt = stmt.limit(limit)
        async with self._factory() as s:
            return list((await s.execute(stmt)).scalars())

    # -------------------------------------------------------------- snapshots
    async def add_snapshot(self, mode: TradingMode, values: dict[str, Decimal | None]) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                s.add(PortfolioSnapshotRow(ts=datetime.now(UTC), mode=mode.value, **values))

        await self._write(_do)

    async def snapshots(
        self, mode: TradingMode, since: datetime | None = None, limit: int = 5000
    ) -> list[PortfolioSnapshotRow]:
        stmt = select(PortfolioSnapshotRow).where(PortfolioSnapshotRow.mode == mode.value)
        if since is not None:
            stmt = stmt.where(PortfolioSnapshotRow.ts >= since)
        stmt = stmt.order_by(PortfolioSnapshotRow.ts).limit(limit)
        async with self._factory() as s:
            return list((await s.execute(stmt)).scalars())

    # -------------------------------------------------------------- bot state
    async def get_state(self, key: str) -> dict[str, Any] | None:
        async with self._factory() as s:
            row = await s.get(BotStateRow, key)
            return dict(row.value) if row is not None else None

    async def get_states_prefix(self, prefix: str) -> dict[str, dict[str, Any]]:
        """All bot_state rows whose key starts with `prefix` (e.g. per-strategy sleeve ledgers)."""
        async with self._factory() as s:
            rows = (
                await s.execute(select(BotStateRow).where(BotStateRow.key.like(f"{prefix}%")))
            ).scalars()
            return {r.key: dict(r.value) for r in rows}

    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                row = await s.get(BotStateRow, key)
                if row is None:
                    s.add(
                        BotStateRow(key=key, value=_jsonable(value), updated_at=datetime.now(UTC))
                    )
                else:
                    row.value = _jsonable(value)
                    row.updated_at = datetime.now(UTC)

        await self._write(_do)

    # --------------------------------------------------------------- commands
    async def add_command(
        self,
        type_: str,
        requested_by: str,
        *,
        confirmed: bool,
        command_id: str | None = None,
        ttl_sec: int | None = None,
        audit: dict[str, Any] | None = None,
    ) -> CommandRow:
        """Idempotent on command_id: re-posting an existing id returns the stored row."""
        cid = command_id or str(uuid.uuid4())
        now = datetime.now(UTC)

        async def _do() -> CommandRow:
            async with session_scope(self._factory) as s:
                existing = (
                    await s.execute(select(CommandRow).where(CommandRow.command_id == cid))
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
                row = CommandRow(
                    command_id=cid,
                    ts=now,
                    type=type_,
                    requested_by=requested_by,
                    confirmed=confirmed,
                    status="PENDING",
                    expires_at=(now + timedelta(seconds=ttl_sec)) if ttl_sec else None,
                    audit=_jsonable(audit or {}),
                )
                s.add(row)
                await s.flush()
                return row

        return await self._write(_do)

    async def pending_commands(self) -> list[CommandRow]:
        async with self._factory() as s:
            return list(
                (
                    await s.execute(
                        select(CommandRow)
                        .where(CommandRow.status == "PENDING")
                        .order_by(CommandRow.id)
                    )
                ).scalars()
            )

    async def update_command(
        self,
        command_id: int,
        *,
        status: str,
        result: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, Any] = {"status": status}
        if status == "ACKNOWLEDGED":
            values["acknowledged_at"] = now
        elif status in ("EXECUTED", "FAILED", "EXPIRED", "REJECTED"):
            values["processed_at"] = now
            if status == "EXECUTED":
                values["executed_at"] = now
            if failure_reason is not None:
                values["failure_reason"] = failure_reason
            if result is not None:
                values["result"] = result

        async def _do() -> None:
            async with session_scope(self._factory) as s:
                await s.execute(
                    update(CommandRow).where(CommandRow.id == command_id).values(**values)
                )

        await self._write(_do)

    async def get_command(self, command_id: str) -> CommandRow | None:
        async with self._factory() as s:
            return (
                await s.execute(select(CommandRow).where(CommandRow.command_id == command_id))
            ).scalar_one_or_none()

    async def recent_commands(self, limit: int = 20) -> list[CommandRow]:
        async with self._factory() as s:
            return list(
                (
                    await s.execute(select(CommandRow).order_by(CommandRow.id.desc()).limit(limit))
                ).scalars()
            )

    # --------------------------------------------------- reconciliation/health
    async def add_reconciliation(self, values: dict[str, Any]) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                s.add(ReconciliationRow(ts=datetime.now(UTC), **values))

        await self._write(_do)

    async def last_reconciliation(self) -> ReconciliationRow | None:
        async with self._factory() as s:
            return (
                await s.execute(
                    select(ReconciliationRow).order_by(ReconciliationRow.id.desc()).limit(1)
                )
            ).scalar_one_or_none()

    async def last_health_snapshot(self) -> HealthSnapshotRow | None:
        async with self._factory() as s:
            return (
                await s.execute(
                    select(HealthSnapshotRow).order_by(HealthSnapshotRow.id.desc()).limit(1)
                )
            ).scalar_one_or_none()

    async def add_health_snapshot(self, values: dict[str, Any]) -> None:
        async def _do() -> None:
            async with session_scope(self._factory) as s:
                s.add(HealthSnapshotRow(ts=datetime.now(UTC), **values))

        await self._write(_do)

    # ---------------------------------------------------------------- backtest
    async def add_backtest_run(
        self,
        *,
        strategy: str,
        params: dict[str, Any],
        timeframe: str,
        start: datetime,
        end: datetime,
        metrics: dict[str, Any],
        equity_curve_path: str | None,
        is_walk_forward: bool = False,
    ) -> int:
        async def _do() -> int:
            async with session_scope(self._factory) as s:
                row = BacktestRunRow(
                    ts=datetime.now(UTC),
                    strategy=strategy,
                    params=_jsonable(params),
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    metrics=metrics,
                    equity_curve_path=equity_curve_path,
                    is_walk_forward=is_walk_forward,
                )
                s.add(row)
                await s.flush()
                return int(row.id)

        return await self._write(_do)

    async def last_backtest_metrics(self) -> dict[str, Any] | None:
        async with self._factory() as s:
            row = (
                await s.execute(
                    select(BacktestRunRow)
                    .where(BacktestRunRow.is_walk_forward.is_(False))
                    .order_by(BacktestRunRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return dict(row.metrics) if row is not None else None

    async def last_backtest_ts(self) -> datetime | None:
        async with self._factory() as s:
            ts = await s.scalar(select(func.max(BacktestRunRow.ts)))
            return _utc(ts) if ts else None
