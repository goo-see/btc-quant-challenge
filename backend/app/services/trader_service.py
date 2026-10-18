"""Trader orchestration for PAPER (and, on Day 4, LIVE via a different gateway).

Pipeline per closed 1m candle:
  persist -> CandleAggregator -> IndicatorSet -> Strategy -> Signal(persist) -> RiskManager(persist)
  -> OrderManager -> PortfolioManager -> events/snapshots
Per ticker: price mark, intrabar stop check (SELL signal through the same risk path).
Supervisor loop: heartbeat, health snapshot, outage classification, MAINTENANCE recovery,
periodic reconciliation, API/DB error kill switches. Command loop: PAUSE / RESUME / KILL.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.core.events import Event, EventBus, EventLevel, EventType
from app.core.logging import get_logger
from app.core.state import BotState, InvalidTransitionError, StateMachine, Transition
from app.core.types import (
    Action,
    Candle,
    OrderIntent,
    OrderRecord,
    OrderState,
    OrdType,
    RiskDecision,
    Side,
    Signal,
    Timeframe,
    TradingMode,
)
from app.database.repository import Repository
from app.exchange.base import ExchangeGateway
from app.exchange.upbit.rest_client import UpbitRestClient
from app.exchange.upbit.ws_client import MessageHandler, WsHealth
from app.execution.order_manager import OrderBlockedError, OrderManager, new_client_order_id
from app.execution.paper_gateway import PaperGateway, PaperLedger
from app.market.candle_engine import CandleAggregator, Live1mTracker
from app.market.historical import HistoricalSync
from app.market.indicators import IndicatorSet
from app.monitoring.daily_report import build_daily_report, save_daily_report
from app.monitoring.health import (
    ExchangeProbe,
    HealthSnapshot,
    OutageClassifier,
    OutageKind,
    ProbeResult,
    RecoveryChecker,
)
from app.monitoring.network import append_observation, observe_public_ipv4
from app.monitoring.slippage import build_execution_record
from app.portfolio.portfolio_manager import PortfolioManager
from app.portfolio.reconciler import Reconciler, ReconcileStatus
from app.portfolio.sleeves import (
    LIVE_LEDGER_MODES,
    SleeveConfig,
    SleeveLedger,
    sizing_caps,
    sleeve_snapshot,
)
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskContext, RiskManager
from app.strategy.base import StrategyContext
from app.strategy.registry import build_strategy

log = get_logger("trader")


class MarketFeed(Protocol):
    health: WsHealth

    async def run(self) -> None: ...

    def stop(self) -> None: ...


FeedFactory = Callable[[MessageHandler], MarketFeed]


def _ws_candle(msg: dict[str, Any], market: str) -> Candle:
    return Candle(
        market=market,
        timeframe=Timeframe.M1,
        open_time=datetime.fromisoformat(str(msg["candle_date_time_utc"])).replace(tzinfo=UTC),
        open=Decimal(str(msg["opening_price"])),
        high=Decimal(str(msg["high_price"])),
        low=Decimal(str(msg["low_price"])),
        close=Decimal(str(msg["trade_price"])),
        volume=Decimal(str(msg["candle_acc_trade_volume"])),
        quote_volume=Decimal(str(msg["candle_acc_trade_price"])),
        is_closed=False,
    )


class TraderService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        gateway: ExchangeGateway,
        *,
        feed_factory: FeedFactory | None = None,
        rest: UpbitRestClient | None = None,
        probe: ExchangeProbe | None = None,
        run_id: str | None = None,
    ) -> None:
        if settings.mode is TradingMode.LIVE and isinstance(gateway, PaperGateway):
            raise RuntimeError("LIVE mode cannot run on the paper gateway")
        if settings.mode is TradingMode.PAPER and not isinstance(gateway, PaperGateway):
            raise RuntimeError("PAPER mode must run on the paper gateway")
        self.settings = settings
        self.cfg = settings.config
        self.mode = settings.mode
        self.repo = repo
        self.gateway = gateway
        self.rest = rest
        self.run_id = (
            run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        self.market = self.cfg.system.market
        self.started_at = datetime.now(UTC)

        self.bus = EventBus()
        self.sm = StateMachine()
        self.kill = KillSwitch(self.sm, repo, self.bus)
        self.portfolio = PortfolioManager(
            repo,
            self.mode,
            self.cfg.challenge.initial_capital_krw,
            report_tz=self.cfg.system.report_timezone,
            strategy_name=self.cfg.strategy.name,
        )
        self.risk = RiskManager(
            self.cfg.risk, self.cfg.risk.for_mode(self.mode), on_kill=self.kill.trigger
        )
        self.orders = OrderManager(gateway, repo, self.bus, self.mode, self.market)
        self.reconciler = Reconciler(
            gateway,
            self.portfolio,
            repo,
            krw_tolerance=self.cfg.kill_switch.reconcile_krw_tolerance,
            btc_tolerance=self.cfg.kill_switch.reconcile_btc_tolerance,
            record_external_flows=self.mode is TradingMode.LIVE,
        )
        self.strategy = build_strategy(
            self.cfg.strategy.name,
            self.cfg.strategy.timeframe,
            self.cfg.strategy.trend_timeframe,
            self.cfg.strategy.params,
        )
        # 1h bars are always built and persisted (continuity of the long 1h history table)
        self._agg_tfs = list(dict.fromkeys([*self.strategy.timeframes, Timeframe.H1]))
        self.aggregator = CandleAggregator(self._agg_tfs)
        self.indicators = {
            tf: IndicatorSet.from_params(self.strategy.params) for tf in self.strategy.timeframes
        }
        self.ctx = StrategyContext()
        self.tracker = Live1mTracker()

        self._feed_factory = feed_factory
        self.feed: MarketFeed | None = None
        self._ws_health = WsHealth()
        self.last_data_at: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.last_reconcile_at: datetime | None = None
        self.last_health_row_at: datetime | None = None
        self._db_errors_seen = 0
        self._order_seq = 0
        self._warming = False
        self._last_warm_open: datetime | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self.last_signal: Signal | None = None
        self.last_decision: dict[str, Any] = {}
        self.running_sec: float = 0.0  # accumulated RUNNING time for this mode (persisted)
        self.epoch_start: str = datetime.now(UTC).isoformat()  # paper evidence window start
        self.last_ip_check_at: datetime | None = None
        self.strategy_epoch: dict[str, Any] = {}
        self._report_day_key: str | None = None
        self.last_execution: dict[str, Any] = {}

        self.classifier = OutageClassifier(
            probe or self._rest_probe, stale_sec=self.cfg.kill_switch.market_data_stale_sec
        )
        self.recovery = RecoveryChecker(
            ws_health=self.ws_health,
            data_age=self.data_age,
            rest_ping=gateway.ping,
            reconcile=lambda: self.reconciler.run(self.orders.inflight()),
            stale_sec=self.cfg.kill_switch.market_data_stale_sec,
        )
        self.bus.subscribe(self._persist_event)
        self.sm.subscribe(self._on_transition)

    # ------------------------------------------------------------ accessors
    def ws_health(self) -> WsHealth:
        return self.feed.health if self.feed is not None else self._ws_health

    def data_age(self, now: datetime | None = None) -> float | None:
        if self.last_data_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.last_data_at).total_seconds()

    async def _rest_probe(self) -> ProbeResult:
        if self.rest is None:
            return ProbeResult(rest_ok=False, exchange_trade_age_sec=None, error="no rest client")
        t = await self.rest.get_ticker(self.market)
        age = datetime.now(UTC).timestamp() - t.trade_timestamp / 1000
        return ProbeResult(rest_ok=True, exchange_trade_age_sec=age)

    # ------------------------------------------------------------ lifecycle
    async def start(self, *, run_loops: bool = True) -> None:
        """`run_loops=False` starts everything except the periodic tasks (deterministic tests)."""
        await self._restore()
        await self.bus.publish(
            Event(
                EventType.BOT_STARTED,
                f"bot started run_id={self.run_id} mode={self.mode}",
                payload={"state": self.sm.state.value},
            )
        )
        await self._warmup()
        if self._feed_factory is not None:
            self.feed = self._feed_factory(self.on_ws_message)
            self._tasks.append(asyncio.create_task(self.feed.run(), name="feed"))
        if run_loops:
            self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="supervisor"))
            self._tasks.append(asyncio.create_task(self._command_loop(), name="commands"))
            self._tasks.append(asyncio.create_task(self._snapshot_loop(), name="snapshots"))

    async def stop(self) -> None:
        self._stop.set()
        if self.feed is not None:
            self.feed.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self._persist_bot_state()
        await self.bus.publish(Event(EventType.BOT_STOPPED, "bot stopped"))

    async def _restore(self) -> None:
        saved = await self.repo.get_state("bot")
        if saved and saved.get("state") == BotState.HALTED.value:
            self.sm.restore(
                BotState.HALTED,
                str(saved.get("reason", "RESTORED_HALTED")),
                datetime.fromisoformat(saved["since"]),
            )
            log.warning("restored_halted_state", reason=self.sm.reason)
        else:
            self.sm.restore(BotState.MAINTENANCE, "STARTUP", datetime.now(UTC))
        if await self.portfolio.load():
            log.info(
                "portfolio_restored",
                krw=str(self.portfolio.state.krw),
                btc=str(self.portfolio.state.btc),
            )
            await self._apply_sleeve_carve_outs()
        gw_state = await self.repo.get_state("gateway")
        if gw_state and isinstance(self.gateway, PaperGateway):
            self.gateway.restore(gw_state)
        rt = await self.repo.get_state(f"runtime_{self.mode.value}") or {}
        self.running_sec = float(rt.get("running_sec", 0.0))
        self.epoch_start = str(rt.get("epoch_start") or datetime.now(UTC).isoformat())
        # Strategy epoch is separate from the infrastructure epoch: a strategy/version change
        # resets paper-trade/signal evidence but keeps runtime/critical/reconcile evidence.
        se = await self.repo.get_state(f"strategy_epoch_{self.mode.value}") or {}
        snapshot = {k: float(v) for k, v in sorted(self.strategy.params.items())}
        phash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]
        sv = self.cfg.strategy.strategy_version
        if se.get("strategy_version") != sv or se.get("parameter_hash") != phash:
            if se:
                # close the previous epoch (kept for history in bot_state.strategy_epoch_history)
                hist = await self.repo.get_state("strategy_epoch_history") or {"epochs": []}
                hist["epochs"] = [
                    *hist.get("epochs", []),
                    {**se, "ended_at": datetime.now(UTC).isoformat()},
                ][-20:]
                await self.repo.set_state("strategy_epoch_history", hist)
            se = {
                "strategy_id": self.strategy.name,
                "strategy_version": sv,
                "strategy": self.strategy.name,  # legacy key
                "version": self.strategy.version,  # legacy key
                "started_at": datetime.now(UTC).isoformat(),
                "parameter_hash": phash,
                "parameter_snapshot": snapshot,
                "timeframe": self.strategy.timeframe.value,
                "trend_timeframe": self.strategy.trend_timeframe.value
                if self.strategy.trend_timeframe
                else None,
                "counters": {
                    "bars_processed": 0,
                    "signals": 0,
                    "buy_sell_signals": 0,
                    "paper_orders": 0,
                    "paper_fills": 0,
                    "paper_trades": 0,
                    "running_sec": 0.0,
                    "critical_errors": 0,
                },
            }
            await self.repo.set_state(f"strategy_epoch_{self.mode.value}", se)
            log.warning("strategy_epoch_started", strategy_version=sv, parameter_hash=phash)
            if self.mode is TradingMode.LIVE:
                await self._init_live_books()
            if self.mode is TradingMode.PAPER and isinstance(self.gateway, PaperGateway):
                # a new PAPER strategy epoch restarts from the virtual principal (books + ledger);
                # the previous epoch's trades/orders/signals stay in the history tables
                initial = self.cfg.challenge.initial_capital_krw
                self.portfolio = PortfolioManager(
                    self.repo,
                    self.mode,
                    initial,
                    report_tz=self.cfg.system.report_timezone,
                    strategy_name=self.strategy.name,
                )
                self.reconciler._pf = self.portfolio
                self.gateway.ledger = PaperLedger(krw=initial)
                await self.repo.set_state("gateway", self.gateway.to_json())
                await self.portfolio.persist()
                log.warning("paper_books_reset", initial=str(initial))
        self.strategy_epoch = se
        await self.orders.load()
        if self.orders.unknown():
            await self.orders.recover_unknown()
        await self._persist_bot_state()

    async def _apply_sleeve_carve_outs(self) -> None:
        """One-time cash transfer from this (D0) book to every OTHER sleeve that holds real assets
        (mode LIVE/LIVE_EXPERIMENT). The wallet is one KRW balance: when the ETH sleeve goes live
        its ₩50,000 stops being D0 cash. Idempotent via bot_state.sleeve_transfers; the open BTC
        position, stop and realized PnL are untouched; principal/HWM/day-start are re-based by the
        same amount so drawdown and daily-loss limits are not distorted by the transfer."""
        pc = self.cfg.portfolio
        own = self._sleeve()
        if not pc.enabled or own is None or self.mode is not TradingMode.LIVE:
            return
        done = await self.repo.get_state("sleeve_transfers") or {}
        for sid, sc in pc.sleeves.items():
            if sid == own[0] or sc.mode not in LIVE_LEDGER_MODES or sid in done:
                continue
            amount = sc.allocated_capital
            st = self.portfolio.state
            before = self.portfolio.to_json()
            if st.krw < amount:
                log.error(
                    "sleeve_carve_out_insufficient_cash",
                    sleeve=sid,
                    amount=str(amount),
                    krw=str(st.krw),
                )
                done[sid] = {
                    "status": "FAILED_INSUFFICIENT_CASH",
                    "amount": str(amount),
                    "at": datetime.now(UTC).isoformat(),
                }
                await self.repo.set_state("sleeve_transfers", done)
                continue
            st.krw = st.krw - amount
            st.initial_equity = st.initial_equity - amount
            st.hwm = max(st.hwm - amount, st.krw)
            self.portfolio.daily.start_equity = self.portfolio.daily.start_equity - amount
            await self.portfolio.persist()
            done[sid] = {
                "status": "DONE",
                "amount": str(amount),
                "from": own[0],
                "to": sid,
                "at": datetime.now(UTC).isoformat(),
                "book_before": before["state"],
                "book_after": self.portfolio.to_json()["state"],
                "position_quantity": str(st.btc),
                "stop_price": str(self.portfolio.stop_price) if self.portfolio.stop_price else None,
            }
            await self.repo.set_state("sleeve_transfers", done)
            # placeholder ledger for the receiving sleeve (its own process overwrites it): the
            # wallet must reconcile from this very restart, before that process is even started
            if not await self.repo.get_state(f"sleeve_ledger_{sid}"):
                placeholder = SleeveLedger(
                    sid,
                    sc.asset,
                    amount,
                    Decimal(0),
                    mode=sc.mode,
                    owner="carve_out_placeholder",
                    updated_at=datetime.now(UTC).isoformat(),
                )
                await self.repo.set_state(
                    placeholder.key(), {**placeholder.to_dict(), "status": "WAITING_PROCESS"}
                )
            await self.bus.publish(
                Event(
                    EventType.SLEEVE_TRANSFER,
                    f"sleeve transfer ₩{amount:,.0f} {own[0]} -> {sid} "
                    f"(book cash now ₩{st.krw:,.0f})",
                    EventLevel.WARN,
                    {k: v for k, v in done[sid].items() if k not in ("book_before", "book_after")},
                )
            )
            log.warning(
                "sleeve_carve_out_done",
                sleeve=sid,
                amount=str(amount),
                krw_after=str(st.krw),
                initial_equity_after=str(st.initial_equity),
            )

    async def _init_live_books(self) -> None:
        """First LIVE start of a strategy epoch: the books start from the EXCHANGE truth.

        Requires BTC = 0 and no open orders (the challenge starts flat); the previous (PAPER) book
        under bot_state['portfolio'] is archived, never mixed. Records challenge.live_activated_at
        (separate from challenge_started_at = first LIVE BUY fill)."""
        bal = await self.gateway.get_balances()
        open_orders = await self.gateway.get_open_orders()
        if bal.btc + bal.btc_locked > 0:
            raise SystemExit(
                f"LIVE_BLOCKED reason=EXCHANGE_BTC_NOT_ZERO btc={bal.btc + bal.btc_locked}"
            )
        if open_orders:
            raise SystemExit(f"LIVE_BLOCKED reason=EXCHANGE_OPEN_ORDERS n={len(open_orders)}")
        start_krw = bal.krw + bal.krw_locked
        initial = self.cfg.challenge.initial_capital_krw
        tol = self.cfg.challenge.funding_tolerance_krw
        if abs(start_krw - initial) > tol:
            raise SystemExit(
                f"LIVE_BLOCKED reason=CHALLENGE_CAPITAL_NOT_FUNDED krw={start_krw} init={initial}"
            )
        prev = await self.repo.get_state("portfolio")
        if prev:
            await self.repo.set_state(
                f"portfolio_archive_{datetime.now(UTC):%Y%m%dT%H%M%S}",
                {**prev, "archived_reason": "LIVE_BOOK_INIT", "archived_mode": "PAPER"},
            )
        self.portfolio = PortfolioManager(
            self.repo,
            self.mode,
            start_krw,  # exact exchange KRW is the accounting principal
            report_tz=self.cfg.system.report_timezone,
            strategy_name=self.strategy.name,
        )
        self.reconciler._pf = self.portfolio
        await self.portfolio.persist()
        ch = await self.repo.get_state("challenge") or {}
        now = datetime.now(UTC).isoformat()
        ch.update(
            {
                "status": "LIVE_ACTIVATED_WAITING_FIRST_BUY",
                "initial_capital": str(initial),
                "initial_challenge_capital": str(initial),
                "live_start_krw": str(start_krw),
                "live_activated_at": ch.get("live_activated_at") or now,
                "live_strategy_version": self.cfg.strategy.strategy_version,
            }
        )
        await self.repo.set_state("challenge", ch)
        log.warning(
            "live_books_initialized", krw=str(start_krw), initial=str(initial), activated=now
        )

    async def _warmup(self) -> None:
        """Prime aggregator/indicators/strategy without look-ahead.

        Stage 1 (only when the strategy uses >=4h bars): replay 1h history for
        `warmup_days_1h` days through a 1h-based aggregator up to the last UTC-day boundary
        before the 1m window. Stage 2: replay 1m history from that boundary through the live
        (1m-based) aggregator. Both stages share indicator sets and strategy state.
        """
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        needs_1h = any(tf.minutes >= 240 for tf in self.strategy.timeframes)
        # Anchor on stored data so replays/tests are deterministic; in production this is ~now.
        m1_min, m1_max, _ = await self.repo.candle_range(self.market, Timeframe.M1)
        _, h1_max, _ = await self.repo.candle_range(self.market, Timeframe.H1)
        end = (
            min(now, (m1_max or h1_max or now) + timedelta(minutes=1))
            if (m1_max or h1_max)
            else now
        )
        if self.rest is not None:
            end = now  # live: always try to fill the tail up to the present
        start_1m = end - timedelta(days=self.cfg.system.warmup_days)
        if m1_min is not None:
            start_1m = max(start_1m, m1_min)
        boundary = start_1m.replace(hour=0, minute=0)  # UTC-day boundary (1d/4h buckets align)
        if m1_min is None or boundary < m1_min.replace(hour=0, minute=0):
            boundary = (m1_min or end).replace(
                hour=0, minute=0
            )  # no 1m before this day: 1h covers it
        if self.rest is not None:
            try:
                if needs_1h:
                    await HistoricalSync(self.rest, self.repo).sync(
                        self.market,
                        boundary - timedelta(days=self.cfg.system.warmup_days_1h),
                        boundary,
                        tf=Timeframe.H1,
                    )
                await HistoricalSync(self.rest, self.repo).sync(self.market, boundary, end)
            except Exception as exc:
                log.warning("warmup_tail_sync_failed", error=repr(exc))
        self._warming = True
        fed_1h = 0
        try:
            if needs_1h:
                h1 = await self.repo.load_candles(
                    self.market,
                    Timeframe.H1,
                    boundary - timedelta(days=self.cfg.system.warmup_days_1h),
                    boundary,
                )
                agg1h = CandleAggregator(self.strategy.timeframes, base=Timeframe.H1)
                for c in h1:
                    fed_1h += 1
                    for closed in agg1h.add(c):
                        self.ctx.indicators[closed.timeframe] = self.indicators[
                            closed.timeframe
                        ].update(closed)
                        self.ctx.position = None
                        self.strategy.on_candle(closed, self.ctx)
                    if c.timeframe in self.strategy.timeframes:
                        self.ctx.indicators[c.timeframe] = self.indicators[c.timeframe].update(c)
                        self.strategy.on_candle(c, self.ctx)
            candles = await self.repo.load_candles(self.market, Timeframe.M1, boundary, end)
            for c in candles:
                for closed in self.aggregator.add(c):
                    if closed.timeframe not in self.indicators:
                        continue  # 1h is aggregated for persistence only
                    self.ctx.indicators[closed.timeframe] = self.indicators[
                        closed.timeframe
                    ].update(closed)
                    self.ctx.position = (
                        self.portfolio.position_view() if self.portfolio.last_price else None
                    )
                    self.strategy.on_candle(closed, self.ctx)
            if candles:
                self.tracker.current = None
                self._last_warm_open = candles[-1].open_time
        finally:
            self._warming = False
        log.info(
            "warmup_done",
            candles_1m=len(candles) if needs_1h or True else 0,
            candles_1h=fed_1h,
            ready={tf.value: ind.ready for tf, ind in self.indicators.items()},
        )

    def _bump(self, key: str, n: float = 1) -> None:
        c = self.strategy_epoch.setdefault("counters", {})
        c[key] = c.get(key, 0) + n

    async def _persist_strategy_epoch(self) -> None:
        if self.strategy_epoch:
            self.strategy_epoch["indicators_ready"] = {
                tf.value: ind.ready for tf, ind in self.indicators.items()
            }
            self.strategy_epoch["stop_state"] = {
                "stop_price": str(self.portfolio.stop_price) if self.portfolio.stop_price else None,
                "has_position": self.portfolio.state.has_position,
                "highest_since_entry": str(self.portfolio.state.highest_since_entry)
                if self.portfolio.state.highest_since_entry
                else None,
            }
            await self.repo.set_state(f"strategy_epoch_{self.mode.value}", self.strategy_epoch)

    # ------------------------------------------------------------ market data
    def _mark_data(self, msg: dict[str, Any], now: datetime) -> None:
        """Freshness = exchange timestamp carried in the message (falls back to local now)."""
        raw = msg.get("trade_timestamp") or msg.get("timestamp")
        ts = (
            datetime.fromtimestamp(int(raw) / 1000, tz=UTC) if isinstance(raw, int | float) else now
        )
        if self.last_data_at is None or ts > self.last_data_at:
            self.last_data_at = ts

    async def on_ws_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        now = datetime.now(UTC)
        if t == "ticker":
            self._mark_data(msg, now)
            price = Decimal(str(msg["trade_price"]))
            raw_ts = msg.get("trade_timestamp")
            ts = (
                datetime.fromtimestamp(int(raw_ts) / 1000, tz=UTC)
                if isinstance(raw_ts, int | float)
                else now
            )
            await self.on_tick(price, ts)
        elif t == "candle.1m":
            self._mark_data(msg, now)
            c = _ws_candle(msg, self.market)
            if self._last_warm_open is not None and c.open_time <= self._last_warm_open:
                return  # already in the warm-up set
            closed = self.tracker.update(c)
            if closed is not None:
                await self.on_closed_1m(closed)

    async def on_tick(self, price: Decimal, ts: datetime) -> None:
        if isinstance(self.gateway, PaperGateway):
            self.gateway.update_price(price, ts)
        self.portfolio.on_price(price, ts)
        stop = self.portfolio.stop_price
        if (
            self.portfolio.state.has_position
            and stop is not None
            and price <= stop
            and self.sm.exits_allowed  # protective exits run in RUNNING/PAUSED/loss-limit HALT
        ):
            sig = Signal(
                action=Action.SELL,
                strength=1.0,
                reason=f"STOP@{stop:.0f} (price {price:.0f})",
                timestamp=ts,
                strategy=self.strategy.name,
                timeframe=self.strategy.timeframe,
                indicators={"tick_price": float(price), "trigger_price": float(stop)},
                stop_price=stop,
            )
            await self.handle_signal(sig)

    async def on_closed_1m(self, c: Candle) -> None:
        await self.repo.upsert_candles([c], source="ws")
        closed = self.aggregator.add(c)
        closed.sort(key=lambda x: -x.timeframe.minutes)
        for hc in closed:
            await self.repo.upsert_candles([hc], source="resample")
            agg = self.strategy_epoch.setdefault("aggregation", {})
            agg[hc.timeframe.value] = {
                "last_close": hc.close_time.isoformat(),
                "bars": int(agg.get(hc.timeframe.value, {}).get("bars", 0)) + 1,
            }
            if hc.timeframe not in self.indicators:
                continue  # 1h is persisted for history only; the strategy does not consume it
            self.ctx.indicators[hc.timeframe] = self.indicators[hc.timeframe].update(hc)
            self.ctx.position = self.portfolio.position_view()
            sig = self.strategy.on_candle(hc, self.ctx)
            if sig is None:
                continue
            if hc.timeframe is self.strategy.timeframe:
                self._bump("bars_processed")
            await self.bus.publish(
                Event(
                    EventType.CANDLE_CLOSED,
                    f"{hc.timeframe.value} closed {hc.close}",
                    payload={"tf": hc.timeframe.value, "close": str(hc.close)},
                )
            )
            await self.handle_signal(sig)

    # ------------------------------------------------------------ signals
    async def handle_signal(self, sig: Signal) -> None:
        self.last_signal = sig
        self._bump("signals")
        if sig.action is not Action.HOLD:
            self._bump("buy_sell_signals")
        signal_id = await self.repo.save_signal(sig, self.mode, self.run_id)
        if self.portfolio.state.has_position:
            before = self.portfolio.stop_price
            self.portfolio.update_stop(sig.stop_price)
            if self.portfolio.stop_price != before:
                await self.portfolio.persist()  # a restart must restore the ratcheted stop
        await self._record_decision(sig)
        if sig.action is Action.HOLD:
            return
        if sig.action is Action.SELL and not self.portfolio.state.has_position:
            # already flat (e.g. the tick stop fired earlier in this bar): analysis-only signal,
            # no execution attempt, no RISK_REJECTED noise
            await self.repo.save_risk_decision(
                signal_id,
                RiskDecision(
                    False, "SUPPRESSED_ALREADY_FLAT", snapshot={"execution": "SUPPRESSED"}
                ),
            )
            return
        await self.bus.publish(
            Event(
                EventType.SIGNAL_GENERATED,
                f"{sig.action.value}: {sig.reason}",
                payload={
                    "signal_id": signal_id,
                    "action": sig.action.value,
                    "strength": sig.strength,
                },
            )
        )
        if self.portfolio.last_price is None:
            log.warning("signal_without_price", action=sig.action.value)
            return
        ctx = await self._risk_context()
        decision = await self.risk.evaluate(sig, ctx)
        await self.repo.save_risk_decision(signal_id, decision)
        if not decision.approved:
            await self.bus.publish(
                Event(
                    EventType.RISK_REJECTED,
                    f"signal {sig.action.value} / execution BLOCKED: {decision.reason}",
                    EventLevel.WARN,
                    {
                        "reason": decision.reason,
                        "signal_id": signal_id,
                        "signal": sig.action.value,
                        "execution": "BLOCKED",
                        "execution_mode": self.sm.execution_mode.value,
                    },
                )
            )
            return
        await self.bus.publish(
            Event(
                EventType.RISK_APPROVED,
                f"{sig.action.value} approved",
                payload={
                    "signal_id": signal_id,
                    "order_krw": str(decision.order_krw),
                    "order_volume": str(decision.order_volume),
                },
            )
        )
        await self._execute(sig, signal_id, decision.order_krw, decision.order_volume)

    async def _record_decision(self, sig: Signal) -> None:
        """Decision snapshot for the 'Why HOLD/BUY/SELL' panel (strategy-TF evals and stops)."""
        ind = sig.indicators
        pf = self.portfolio
        pos = pf.position_view()
        ex = self.sm.execution_mode
        trend_ok = (
            (ind["close_trend"] > ind["trend_ema"])
            if ("close_trend" in ind and "trend_ema" in ind)
            else None
        )
        upper = ind.get("channel_upper")
        lower = ind.get("channel_lower")
        close_tf = ind.get("close")
        atr = ind.get("atr")
        trail = None
        if pos is not None and atr is not None:
            trail = float(pos.highest_since_entry) - float(atr) * float(
                self.strategy.params.get("atr_trail_mult", 4.0)
            )
        snap: dict[str, Any] = {
            "strategy_version": self.cfg.strategy.strategy_version,
            "evaluated_at": datetime.now(UTC).isoformat(),
            "market_bar_time": sig.timestamp.isoformat(),
            "timeframe": sig.timeframe.value,
            "decision": sig.action.value,
            "reason": sig.reason,
            "daily_close": ind.get("close_trend"),
            "daily_ema": ind.get("trend_ema"),
            "trend_ok": trend_ok,
            "close_4h": close_tf,
            "donchian_upper": upper,
            "breakout_ok": (close_tf > upper)
            if (close_tf is not None and upper is not None)
            else None,
            "donchian_lower": lower,
            "atr": atr,
            "position_quantity": str(pf.state.btc) if pf.state.has_position else None,
            "entry_price": str(pf.state.avg_entry) if pf.state.avg_entry else None,
            "initial_stop": str(sig.stop_price)
            if (sig.action is Action.BUY and sig.stop_price)
            else None,
            "current_stop": str(pf.stop_price) if pf.stop_price else None,
            "trailing_stop": trail,
            "channel_exit": lower,
            "trend_loss": (not trend_ok) if trend_ok is not None else None,
            "risk_status": {
                "state": self.sm.state.value,
                "state_reason": self.sm.reason,
                "execution_mode": ex.value,
                "inflight": len(self.orders.inflight()),
                "unknown": len(self.orders.unknown()),
                "daily_pnl": str(pf.daily_pnl()) if pf.last_price else None,
                "drawdown": str(pf.drawdown()) if pf.last_price else None,
            },
            "current_price": str(pf.last_price) if pf.last_price else None,
        }
        self.last_decision = snap
        with contextlib.suppress(Exception):
            await self.repo.set_state(f"decision_{self.mode.value}", snap)

    async def _risk_context(self) -> RiskContext:
        now = datetime.now(UTC)
        pf = self.portfolio
        equity, krw = pf.equity(), pf.state.krw
        sleeve = self._sleeve()
        if sleeve is not None:
            # sleeve isolation: min(book, allocation); never spend another sleeve's reserve
            equity, krw = sizing_caps(
                sleeve[1].allocated_capital,
                equity,
                krw,
                pf.state.btc_value(pf.price) if pf.state.has_position else Decimal(0),
            )
        return RiskContext(
            now=now,
            bot_state=self.sm.state,
            bot_reason=self.sm.reason,
            price=pf.price,
            equity=equity,
            krw=krw,
            btc=pf.state.btc,
            has_position=pf.state.has_position,
            open_positions=1 if pf.state.has_position else 0,
            daily_pnl=pf.daily_pnl(),
            day_start_equity=pf.daily.start_equity,
            drawdown=pf.drawdown(),
            consecutive_losses=pf.consecutive_losses,
            last_loss_at=pf.last_loss_at,
            orders_last_hour=await self.repo.count_orders_since(
                self.mode, now - timedelta(hours=1)
            ),
            inflight_orders=len(self.orders.inflight()),
            unknown_orders=len(self.orders.unknown()),
        )

    def _sleeve(self) -> tuple[str, SleeveConfig] | None:
        pc = self.cfg.portfolio
        if not pc.enabled:
            return None
        return pc.for_strategy(self.cfg.strategy.strategy_version)

    async def _persist_sleeve(self) -> None:
        """bot_state.strategy_sleeves: this strategy's sleeve snapshot + ledger (owner=trader)."""
        sleeve = self._sleeve()
        if sleeve is None or self.portfolio.last_price is None:
            return
        sid, sc = sleeve
        pf = self.portfolio
        snap = sleeve_snapshot(
            sid,
            sc,
            cash=pf.state.krw,
            quantity=pf.state.btc,
            price=pf.last_price,
            realized_pnl=pf.state.realized_pnl,
            fees=pf.state.fees_total,
            entry_fees=pf.state.entry_fees,
            avg_entry=pf.state.avg_entry,
            max_position_pct=self.cfg.risk.for_mode(self.mode).max_position_pct,
        )
        ledger = SleeveLedger(
            sid,
            sc.asset,
            pf.state.krw,
            pf.state.btc,
            mode=self.mode.value,
            owner="trader",
            inflight=bool(self.orders.inflight()),
            updated_at=datetime.now(UTC).isoformat(),
        )
        with contextlib.suppress(Exception):
            await self.repo.set_state(ledger.key(), ledger.to_dict())
            state = await self.repo.get_state("strategy_sleeves") or {}
            sleeves = state.get("sleeves", {})
            sleeves[sid] = snap
            await self.repo.set_state(
                "strategy_sleeves",
                {**{k: v for k, v in state.items() if k != "ledgers"}, "sleeves": sleeves},
            )

    async def _execute(
        self, sig: Signal, signal_id: int, krw: Decimal | None, volume: Decimal | None
    ) -> None:
        self._order_seq += 1
        side = Side.BID if sig.action is Action.BUY else Side.ASK
        intent = OrderIntent(
            client_order_id=new_client_order_id(self.run_id, self._order_seq, side),
            market=self.market,
            side=side,
            ord_type=OrdType.PRICE if side is Side.BID else OrdType.MARKET,
            krw_amount=krw,
            volume=volume,
            signal_id=signal_id,
            mode=self.mode,
        )
        reference_price = self.portfolio.last_price
        entry_price_before = self.portfolio.state.avg_entry
        entry_time_before = self.portfolio.state.entry_time
        try:
            rec = await self.orders.submit(intent)
        except OrderBlockedError as exc:
            await self.bus.publish(
                Event(
                    EventType.ORDER_REJECTED,
                    f"order blocked: {exc.reason}",
                    EventLevel.WARN,
                    {"reason": exc.reason},
                )
            )
            return
        self._bump("paper_orders" if self.mode is TradingMode.PAPER else "orders")
        if rec.fills:
            self._bump("paper_fills" if self.mode is TradingMode.PAPER else "fills", len(rec.fills))
            if side is Side.ASK:
                self._bump("paper_trades" if self.mode is TradingMode.PAPER else "trades")
            if side is Side.ASK and self.portfolio.state.avg_entry is not None:
                rec.raw["entry_price"] = str(self.portfolio.state.avg_entry)
                rec.raw["entry_fees"] = str(self.portfolio.state.entry_fees)
            realized = await self.portfolio.apply_order(rec, reason=sig.reason, stop=sig.stop_price)
            await self._record_execution(
                rec,
                sig,
                reference_price=reference_price,
                realized=realized,
                entry_price=entry_price_before,
                entry_time=entry_time_before,
            )
            if self.mode is TradingMode.LIVE and side is Side.BID:
                await self._record_challenge_start(rec.fills[0].timestamp)
            ev = EventType.POSITION_OPENED if side is Side.BID else EventType.POSITION_CLOSED
            await self.bus.publish(
                Event(
                    ev,
                    f"{ev.value} {rec.executed_volume} BTC @ {rec.avg_fill_price}",
                    payload={
                        "realized_pnl": str(realized) if realized is not None else None,
                        "client_order_id": rec.client_order_id,
                    },
                )
            )
            if isinstance(self.gateway, PaperGateway):
                await self.repo.set_state("gateway", self.gateway.to_json())
            await self.portfolio.save_snapshot()
        if rec.state is OrderState.UNKNOWN:
            await self.kill.trigger(
                "ORDER_UNKNOWN_UNRESOLVED", {"client_order_id": rec.client_order_id}
            )
        if self.orders.consecutive_api_errors >= self.cfg.kill_switch.api_error_consecutive:
            await self.kill.trigger(
                "API_ERRORS", {"consecutive": self.orders.consecutive_api_errors}
            )

    async def _record_execution(
        self,
        rec: OrderRecord,
        sig: Signal,
        *,
        reference_price: Decimal | None,
        realized: Decimal | None,
        entry_price: Decimal | None,
        entry_time: datetime | None,
    ) -> None:
        """Persist execution quality (slippage, stop distances, exit economics) per filled order."""
        try:
            row = build_execution_record(
                rec=rec,
                sig=sig,
                strategy_version=self.cfg.strategy.strategy_version,
                reference_price_at_send=reference_price,
                atr_trail_mult=float(self.strategy.params.get("atr_trail_mult", 4.0)),
                realized_pnl=realized,
                entry_price=entry_price,
                entry_time=entry_time,
            )
            await self.repo.add_execution_metric(row)
            self.last_execution = {
                k: (
                    str(v)
                    if isinstance(v, Decimal)
                    else v.isoformat()
                    if isinstance(v, datetime)
                    else v
                )
                for k, v in row.items()
                if k != "extra"
            }
            log.warning(
                "execution_recorded",
                side=row["side"],
                fill=str(row["actual_fill_price"]),
                slippage_bps=str(row["slippage_bps"]),
                fee=str(row["fee"]),
            )
        except Exception as exc:
            log.error("execution_metric_failed", error=repr(exc))

    # ------------------------------------------------------------ supervisor
    async def supervisor_tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self.last_heartbeat is not None and self.sm.state is BotState.RUNNING:
            delta = (now - self.last_heartbeat).total_seconds()
            if 0 < delta <= self.cfg.system.heartbeat_sec * 3:  # ignore gaps (sleep/crash)
                self.running_sec += delta
                self._bump("running_sec", delta)
        self.last_heartbeat = now
        await self._persist_bot_state()
        if (
            self.last_health_row_at is None
            or (now - self.last_health_row_at).total_seconds()
            >= self.cfg.system.snapshot_interval_sec
        ):
            self.last_health_row_at = now
            await self._persist_health(now)
        if (
            self.repo.write_errors - self._db_errors_seen
            >= self.cfg.kill_switch.db_write_error_limit
        ):
            self._db_errors_seen = self.repo.write_errors
            await self.kill.trigger("DATABASE_FAILURE", {"write_errors": self.repo.write_errors})
        await self._maybe_observe_public_ip(now)
        await self._maybe_daily_report()
        state = self.sm.state
        if state is BotState.HALTED:
            return
        if state is BotState.MAINTENANCE:
            rep = await self.recovery.run(now)
            if rep.all_passed:
                await self._transition(BotState.RUNNING, "RECOVERY_CHECKS_PASSED")
            elif rep.reconciliation_failed:
                await self.kill.trigger("RECONCILE_FAILED_DURING_RECOVERY", {"failed": rep.failed})
            else:
                log.info("recovery_pending", failed=rep.failed)
            return
        # RUNNING / PAUSED
        age = self.data_age(now)
        stale = self.cfg.kill_switch.market_data_stale_sec
        if age is None or age >= stale:
            kind = await self.classifier.classify(self.ws_health(), age)
            if kind is OutageKind.EXCHANGE_OUTAGE:
                await self._transition(BotState.MAINTENANCE, "EXCHANGE_OUTAGE")
            elif kind is OutageKind.LOCAL_FAULT:
                if (
                    age is not None
                    and age >= stale * self.cfg.kill_switch.local_fault_grace_multiplier
                ):
                    await self.kill.trigger("UNEXPLAINED_STALE_LOCAL_FAULT", {"data_age_sec": age})
                else:
                    log.warning("local_feed_stale", data_age_sec=age)
            elif kind is OutageKind.UNEXPLAINED:
                await self.kill.trigger("UNEXPLAINED_STALE", {"data_age_sec": age})
            return
        if self.orders.consecutive_api_errors >= self.cfg.kill_switch.api_error_consecutive:
            await self.kill.trigger(
                "API_ERRORS", {"consecutive": self.orders.consecutive_api_errors}
            )
            return
        due = (
            self.last_reconcile_at is None
            or (now - self.last_reconcile_at).total_seconds()
            >= self.cfg.system.reconcile_interval_sec
        )
        if due and not self.orders.inflight():
            self.last_reconcile_at = now
            res = await self.reconciler.run(self.orders.inflight())
            if res.ok:
                await self.bus.publish(
                    Event(EventType.RECONCILE_OK, "reconcile ok", payload=res.details)
                )
            elif res.status is ReconcileStatus.WARN:
                # another LIVE strategy's ledger is settling: judged again on the next run
                self.last_reconcile_at = now - timedelta(
                    seconds=max(0, self.cfg.system.reconcile_interval_sec - 30)
                )
                await self.bus.publish(
                    Event(
                        EventType.RECONCILE_DEFERRED,
                        "reconcile deferred (other strategy settling)",
                        EventLevel.WARN,
                        res.details,
                    )
                )
            else:
                await self.bus.publish(
                    Event(
                        EventType.RECONCILE_MISMATCH,
                        "reconcile mismatch",
                        EventLevel.CRITICAL,
                        res.details,
                    )
                )
                await self.kill.trigger("RECONCILE_FAILED", res.details)

    async def _record_challenge_start(self, filled_at: datetime) -> None:
        """challenge_started_at = the first LIVE BUY FILL time (NOT the LIVE activation time,
        which is kept separately as live_activated_at)."""
        ch = await self.repo.get_state("challenge") or {}
        if ch.get("start_date"):
            return
        kst = filled_at.astimezone(ZoneInfo(self.cfg.system.report_timezone))
        await self.repo.set_state(
            "challenge",
            {
                **ch,
                "status": "STARTED",
                "initial_capital": str(self.cfg.challenge.initial_capital_krw),
                "initial_challenge_capital": str(self.cfg.challenge.initial_capital_krw),
                "start_date": kst.date().isoformat(),
                "challenge_started_at": filled_at.isoformat(),
                "first_fill_at": filled_at.isoformat(),
                "live_activated_at": ch.get("live_activated_at") or self.started_at.isoformat(),
            },
        )
        log.warning("challenge_started", start_date=kst.date().isoformat())

    async def _maybe_observe_public_ip(self, now: datetime) -> None:
        """Hourly outbound public IPv4 observation feeding the STATIC_PUBLIC_IP gate."""
        interval = self.cfg.live_gate.public_ip_check_interval_sec
        if (
            self.last_ip_check_at is not None
            and (now - self.last_ip_check_at).total_seconds() < interval
        ):
            return
        self.last_ip_check_at = now
        if self._feed_factory is None:  # offline unit tests: never hit the network
            return
        try:
            ip, detail = await observe_public_ipv4()
        except Exception as exc:
            log.warning("public_ip_check_failed", error=repr(exc))
            return
        state = await self.repo.get_state("public_ip") or {}
        history = append_observation(list(state.get("history", [])), ip, detail)
        await self.repo.set_state(
            "public_ip", {"history": history, "latest": ip, "at": now.isoformat()}
        )

    async def _maybe_daily_report(self) -> None:
        """At the first tick of a new KST day, write the report for the previous day."""
        key = self.portfolio.daily.day_key
        if self._report_day_key is None:
            self._report_day_key = key
            return
        if key == self._report_day_key:
            return
        prev = self._report_day_key
        self._report_day_key = key
        try:
            await self.generate_daily_report(datetime.fromisoformat(prev).date())
        except Exception as exc:
            log.error("daily_report_failed", day=prev, error=repr(exc))

    async def generate_daily_report(self, day: date) -> Path:
        ch = await self.repo.get_state("challenge") or {}
        start = ch.get("start_date") or self.cfg.challenge.start_date
        rep = await build_daily_report(
            self.repo,
            mode=self.mode,
            day=day,
            tz=self.cfg.system.report_timezone,
            initial_equity=self.cfg.challenge.initial_capital_krw,
            challenge_start=datetime.fromisoformat(start).date() if start else None,
        )
        path = await save_daily_report(self.repo, rep, self.cfg.system.reports_dir)
        await self.bus.publish(
            Event(
                EventType.DAILY_REPORT,
                f"daily report {rep.date}: {rep.daily_pnl:+,.0f} KRW",
                payload={"path": str(path)},
            )
        )
        return path

    async def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.supervisor_tick()
            except Exception as exc:
                log.error("supervisor_error", error=repr(exc))
            await asyncio.sleep(self.cfg.system.heartbeat_sec)

    async def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.system.snapshot_interval_sec)
            with contextlib.suppress(Exception):
                await self.portfolio.save_snapshot()
                await self.portfolio.persist()

    # ------------------------------------------------------------ commands
    async def process_commands(self, now: datetime | None = None) -> None:
        """PENDING -> ACKNOWLEDGED -> EXECUTED|FAILED ; expired/unconfirmed -> EXPIRED|REJECTED.

        TTL: a command older than its expires_at (set by the API from config) is never executed —
        this is what keeps a stale KILL/RESUME from firing after a trader restart.
        """
        now = now or datetime.now(UTC)
        for cmd in await self.repo.pending_commands():
            exp = cmd.expires_at
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp is not None and now > exp:
                await self.repo.update_command(
                    cmd.id, status="EXPIRED", failure_reason="ttl expired"
                )
                log.warning("command_expired", command_id=cmd.command_id, type=cmd.type)
                continue
            if not cmd.confirmed:
                await self.repo.update_command(
                    cmd.id, status="REJECTED", failure_reason="confirmation required"
                )
                continue
            await self.repo.update_command(cmd.id, status="ACKNOWLEDGED")
            try:
                result = await self.apply_command(cmd.type)
            except InvalidTransitionError as exc:
                await self.repo.update_command(cmd.id, status="FAILED", failure_reason=str(exc))
                continue
            if result.startswith("REJECTED"):
                await self.repo.update_command(cmd.id, status="FAILED", failure_reason=result)
            else:
                await self.repo.update_command(cmd.id, status="EXECUTED", result=result)
            await self.repo.add_event(
                "WARN",
                "COMMAND",
                f"{cmd.type} by {cmd.requested_by}: {result}",
                {"command_id": cmd.command_id, "audit": dict(cmd.audit or {})},
            )

    async def apply_command(self, type_: str) -> str:
        if type_ == "PAUSE" and self.sm.state is BotState.PAUSED:
            return "REJECTED: already PAUSED"
        if type_ == "RESUME" and self.sm.state in (BotState.RUNNING, BotState.MAINTENANCE):
            return f"REJECTED: already {self.sm.state.value}"
        if type_ == "KILL" and self.sm.state is BotState.HALTED:
            return "REJECTED: already HALTED"
        if type_ == "PAUSE":
            await self._transition(BotState.PAUSED, "MANUAL_PAUSE", manual=True)
            await self.bus.publish(Event(EventType.BOT_PAUSED, "paused by operator"))
            return "OK"
        if type_ == "RESUME":
            # Resume always goes through MAINTENANCE so recovery checks run first.
            await self._transition(BotState.MAINTENANCE, "MANUAL_RESUME", manual=True)
            await self.bus.publish(
                Event(EventType.BOT_RESUMED, "resume requested; running recovery checks")
            )
            return "OK"
        if type_ == "KILL":
            await self.kill.trigger("MANUAL_KILL", {})
            return "OK"
        return f"REJECTED: unknown command {type_}"

    async def _command_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.process_commands()
            await asyncio.sleep(self.cfg.system.command_poll_sec)

    # ------------------------------------------------------------ persistence
    async def _transition(self, to: BotState, reason: str, *, manual: bool = False) -> None:
        if self.sm.state is to:
            return
        await self.sm.transition(to, reason, manual=manual)

    async def _on_transition(self, t: Transition) -> None:
        log.warning(
            "bot_state", from_state=t.from_state.value, to_state=t.to_state.value, reason=t.reason
        )
        await self._persist_bot_state()

    async def _persist_bot_state(self) -> None:
        await self.repo.set_state(
            "bot",
            {
                "state": self.sm.state.value,
                "reason": self.sm.reason,
                "since": self.sm.since.isoformat(),
                "run_id": self.run_id,
                "mode": self.mode.value,
                "started_at": self.started_at.isoformat(),
                "heartbeat": (self.last_heartbeat or datetime.now(UTC)).isoformat(),
                "strategy": self.strategy.name,
                "pid": os.getpid(),
            },
        )
        await self.repo.set_state(
            "market",
            {
                "price": str(self.portfolio.last_price) if self.portfolio.last_price else None,
                "price_at": self.portfolio.last_price_at.isoformat()
                if self.portfolio.last_price_at
                else None,
                "last_data_at": self.last_data_at.isoformat() if self.last_data_at else None,
            },
        )
        await self._persist_strategy_epoch()
        await self._persist_sleeve()
        await self.repo.set_state(
            f"runtime_{self.mode.value}",
            {
                "running_sec": round(self.running_sec, 1),
                "epoch_start": self.epoch_start,
                "updated": datetime.now(UTC).isoformat(),
            },
        )

    async def _persist_health(self, now: datetime) -> None:
        ws = self.ws_health()
        snap = HealthSnapshot(
            ts=now,
            ws_connected=ws.connected,
            data_age_sec=self.data_age(now),
            rest_ok=self.orders.consecutive_api_errors == 0,
            rest_latency_ms=self.rest.metrics.latency_ema_ms if self.rest else 0.0,
            rest_consecutive_errors=self.rest.metrics.consecutive_errors if self.rest else 0,
            db_ok=True,
            heartbeat_age_sec=0.0,
            reconnect_count=ws.reconnect_count,
            message_count=ws.message_count,
        )
        with contextlib.suppress(Exception):
            await self.repo.add_health_snapshot(
                snap.to_row(int((now - self.started_at).total_seconds()), self.last_data_at, now)
            )

    async def _persist_event(self, ev: Event) -> None:
        if ev.level is EventLevel.CRITICAL:
            self._bump("critical_errors")
        with contextlib.suppress(Exception):
            await self.repo.add_event(ev.level.value, ev.type.value, ev.message, ev.payload)


__all__ = ["FeedFactory", "MarketFeed", "TraderService"]
_ = Awaitable  # re-exported typing helper kept for feed factories
