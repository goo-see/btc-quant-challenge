"""EthGridService — the ETH grid LIVE EXPERIMENT process (separate from the BTC trader).

    Upbit WS ticker KRW-ETH -> LiveGridEngine.next_intent -> GridRisk -> OrderManager ->
    LiveGateway(KRW-ETH) -> apply real fill -> persist (own DB) -> sleeve ledger (main DB)

Own database (eth_grid_live.db): orders/fills (OrderManager), bot_state eth_grid_live (engine
state, health, reconcile), eth_grid_cycles, eth_grid_control (enable / unfreeze), events (Korean).
Main database (quant.db): ONLY bot_state 'sleeve_ledger_ETH_GRID' (cash, ETH qty, in-flight
flag) so the BTC trader's reconciliation can account for this sleeve; the D0 book is read-only here.
Write-ahead order flow: pending (state) -> ledger inflight -> OrderManager (DB row before the
exchange call) -> fill -> state -> ledger. A crash anywhere is recovered by `_recover_pending`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config.settings import Settings
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.core.types import OrderIntent, OrderRecord, OrderState, OrdType, Side, TradingMode
from app.database.repository import Repository
from app.eth_grid.compounding import CompoundingPolicy
from app.eth_grid.live_engine import (
    GridEvent,
    GridStatus,
    Intent,
    LiveGridEngine,
    LiveGridState,
    live_params,
    policy_from_params,
)
from app.eth_grid.risk import GridRisk, GridRiskContext
from app.eth_grid.trend import DailyTrendGuard
from app.exchange.base import ExchangeGateway, GatewayNetworkError, OrderNotFoundError
from app.exchange.upbit.rest_client import UpbitRestClient
from app.execution.order_manager import OrderBlockedError, OrderManager
from app.portfolio.sleeves import SleeveLedger, reconcile_multi_asset
from app.research.eth_grid.engine import KST, GridParams
from app.services.trader_service import FeedFactory, MarketFeed

log = get_logger("eth_grid")

BalanceSource = Callable[
    [], Awaitable[dict[str, Decimal]]
]  # {"KRW": total, "BTC": qty, "ETH": qty}

STATE_KEY = "eth_grid_live"
CYCLES_KEY = "eth_grid_cycles"
CONTROL_KEY = "eth_grid_control"
LEDGER_ID = "ETH_GRID"
RECHECK_DELAY_SEC = 3.0
MAX_INTENTS_PER_OBSERVATION = 12


def _krw(v: Any) -> str:
    try:
        return f"₩{Decimal(str(v)):,.0f}"
    except Exception:
        return str(v)


def _pnl(v: Any) -> str:
    d = Decimal(str(v))
    return f"{'+' if d > 0 else '-' if d < 0 else ''}₩{abs(d):,.0f}"


def korean_event(ev: GridEvent) -> tuple[str, str, str]:
    """GridEvent -> (event type, level, Korean message) for the activity timeline."""
    d = ev.detail
    k = ev.kind
    if k == "SEED":
        return (
            "ETH_SEED_BUY",
            "WARN",
            f"ETH 초기 매수 체결 {_krw(d['funds'])} @ {_krw(d['fill'])} · {d['qty']} ETH · "
            f"수수료 {_krw(d['fee'])} · {d['lots']}개 lot 분할",
        )
    if k == "BUY":
        return (
            "ETH_GRID_BUY",
            "INFO",
            f"Grid 매수 {_krw(d['funds'])} @ {_krw(d['fill'])} · 목표 매도가 {_krw(d['target'])}",
        )
    if k == "SELL":
        return (
            "ETH_GRID_SELL",
            "INFO",
            f"Grid 매도 {_krw(d['funds'])} @ {_krw(d['fill'])} · 순익 {_pnl(d['net'])} · "
            f"Cycle #{d['cycles']} 완료",
        )
    if k in ("MDD_5", "MDD_10"):
        return (
            f"ETH_{k}",
            "WARN",
            f"Grid 자산 고점 대비 -{k.split('_')[1]}% 도달 (현재 {_krw(d['equity'])})",
        )
    if k == "HALT":
        return (
            "ETH_MDD_HALT",
            "ERROR",
            f"Grid MDD 12% 도달 — 신규 매수 중단, 보유분 매도만 허용 ({d['reason']})",
        )
    if k == "GUARD_ON":
        return (
            "ETH_BUY_PAUSE_DOWNTREND",
            "WARN",
            "일봉 종가 EMA100 하회 — 신규 Grid 매수 일시중지 "
            f"(종가 {_krw(d['close'])} / EMA {_krw(d['ema'])})",
        )
    if k == "GUARD_OFF":
        return (
            "ETH_BUY_RESUME_TREND",
            "INFO",
            "일봉 종가 EMA100 회복 — Grid 매수 재개 "
            f"(종가 {_krw(d['close'])} / EMA {_krw(d['ema'])})",
        )
    if k == "PAUSE":
        return (
            "ETH_DAILY_PAUSE",
            "WARN",
            f"오늘 Grid 손실 -5% 도달 — 오늘 신규 매수 중지 (자산 {_krw(d['equity'])})",
        )
    if k == "RESUME":
        return ("ETH_DAILY_RESUME", "INFO", "새 날(KST) 시작 — Grid 매수 재개")
    if k == "RECENTER":
        return (
            "ETH_RECENTER",
            "INFO",
            f"Grid 기준가 재설정 {_krw(d['anchor'])} (보유 lot {d['lots']}개 유지)",
        )
    if k == "FROZEN":
        return (
            "ETH_FROZEN",
            "ERROR",
            f"ETH Grid FROZEN — 자동 주문 중단: {d['reason']} (사용자 확인 필요)",
        )
    if k == "UNFROZEN":
        return ("ETH_UNFROZEN", "WARN", f"ETH Grid FROZEN 해제 ({d['by']})")
    if k == "BUY_BLOCKED":
        return (
            "ETH_BUY_BLOCKED",
            "INFO",
            f"Grid 매수 보류: {d['reason']} (레벨 {d['level']} {_krw(d['price'])})",
        )
    if k == "ORDER_NOT_FILLED":
        return ("ETH_ORDER_NOT_FILLED", "WARN", f"주문 미체결/거부: {d['reason']}")
    if k == "PARTIAL_SELL":
        return (
            "ETH_PARTIAL_SELL",
            "WARN",
            f"부분 매도: lot {d['lot_id']} {d['sold']} ETH 매도, {d['remaining']} ETH 잔여",
        )
    if k == "BUY_PAUSED":
        return (
            "ETH_BUY_PAUSED",
            "WARN",
            f"신규 ETH 매수 일시중단 ({d['reason']}, {d['by']}) · "
            f"보유 {d['lots']}개 lot은 목표가 도달 시 자동 매도 유지 · 강제 청산 없음",
        )
    if k == "BUY_RESUMED":
        return ("ETH_BUY_RESUMED", "WARN", f"신규 ETH 매수 재개 ({d['by']}, 이전 {d['previous']})")
    if k == "CUTOVER":
        return (
            "ETH_STRATEGY_CUTOVER",
            "WARN",
            f"ETH Grid 신규 매수 규칙 전환 {d['from_epoch']} ({float(d['from_gap']) * 100:.2f}%) → "
            f"{d['to_epoch']} ({float(d['to_gap']) * 100:.2f}%) · "
            f"기준가 {_krw(d['activation_price'])} · "
            f"기존 {d['legacy_lots']}개 lot은 매도 전용(목표가 유지) · 주문 없음",
        )
    if k == "SIZING_UPDATE":
        return (
            "ETH_SIZING_UPDATE",
            "WARN",
            f"복리 주문 단위 변경 {_krw(d['prev_unit'])} → {_krw(d['unit'])} "
            f"(실현 기준 자본 {_krw(d['capital'])}, epoch {d['epoch']})"
            + (" · 최소 주문 미달로 신규 매수 중지" if d.get("paused") else ""),
        )
    if k == "DAY_CLOSE":
        return (
            "ETH_DAY_CLOSE",
            "INFO",
            f"{d['day']} 마감 — Grid 자산 {_krw(d['equity'])} · "
            f"매수 {d['buys']} / 매도 {d['sells']}",
        )
    return (f"ETH_{k}", "INFO", f"{k} {d}")


class EthGridService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        main_repo: Repository,
        gateway: ExchangeGateway,
        *,
        balances: BalanceSource,
        feed_factory: FeedFactory | None = None,
        rest: UpbitRestClient | None = None,
        params: GridParams | None = None,
        trend: DailyTrendGuard | None = None,
        exchange_terms: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        if settings.mode is not TradingMode.LIVE:
            raise RuntimeError("the ETH grid LIVE EXPERIMENT runs only with TRADING_MODE=LIVE")
        self.settings = settings
        self.cfg = settings.config.eth_grid
        self.ks = settings.config.kill_switch
        sleeve = settings.config.portfolio.sleeves.get(LEDGER_ID)
        if sleeve is None or sleeve.mode != "LIVE_EXPERIMENT":
            raise RuntimeError("portfolio.sleeves.ETH_GRID.mode must be LIVE_EXPERIMENT")
        self.sleeve = sleeve
        self.market = self.cfg.market
        self.p = params or live_params(self.cfg.candidate)
        if self.p.budget_krw != sleeve.allocated_capital:
            raise RuntimeError(
                f"candidate budget {self.p.budget_krw} != "
                f"sleeve allocation {sleeve.allocated_capital}"
            )
        self.repo = repo
        self.main_repo = main_repo
        self.gateway = gateway
        self.rest = rest
        self.balances = balances
        self.trend = trend or DailyTrendGuard(self.p.ema_period, market=self.market)
        self.run_id = (
            run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
        )
        self.started_at = datetime.now(UTC)
        self.bus = EventBus()
        self.orders = OrderManager(gateway, repo, self.bus, TradingMode.LIVE, self.market)
        self.risk = GridRisk(self.cfg)
        terms = exchange_terms or {}
        self.min_total = Decimal(str(terms.get("min_total", "5000")))
        cc = self.cfg.compounding
        self.policy: CompoundingPolicy = policy_from_params(
            self.p,
            enabled=cc.enabled,
            round_krw=cc.round_krw,
            max_step_up_pct=cc.max_step_up_pct,
            max_single_order_krw=cc.max_single_grid_order_krw,
            min_total=self.min_total,
        )
        self.engine = LiveGridEngine(self.p, policy=self.policy)
        self.target_epoch = self.cfg.strategy_epoch
        self.target_gap: Decimal = self.cfg.active_gap_pct or (self.p.gap_pct or Decimal(0))
        self.legacy_label = self.cfg.legacy_epoch_label
        self.fee_rate = max(
            Decimal(str(terms.get("bid_fee", "0"))),
            Decimal(str(terms.get("ask_fee", "0"))),
            self.p.fee_rate,
        )
        self._feed_factory = feed_factory
        self.feed: MarketFeed | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self.enabled = False
        self.enabled_at: str | None = None
        self.ready = False
        self.ticks = 0
        self.last_price: Decimal | None = None
        self.last_data_at: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.last_reconcile_at: datetime | None = None
        self.last_trend_at: datetime | None = None
        self.last_control_at: datetime | None = None
        self.bal: dict[str, Decimal] = {}
        self.bal_at: datetime | None = None
        self.reconcile_fails = 0
        self.reconcile_last: dict[str, Any] = {}
        self.consecutive_rejects = 0
        self._db_errors_seen = 0
        self._main_db_errors = 0
        self.errors = 0
        self.last_error: str | None = None
        self.bus.subscribe(self._persist_bus_event)

    # ------------------------------------------------------------------ properties
    @property
    def epoch(self) -> str:
        st = self.engine.s.started_at or self.started_at
        return st.strftime("%Y%m%d%H%M%S")

    def data_age(self, now: datetime | None = None) -> float | None:
        if self.last_data_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.last_data_at).total_seconds()

    @property
    def status(self) -> GridStatus:
        return self.engine.status

    # ------------------------------------------------------------------ lifecycle
    async def start(self, *, run_loops: bool = True) -> None:
        await self._restore()
        await self._poll_control(force=True)
        await self._recover_pending()
        await self._bootstrap_trend()
        await self._preflight()
        await self._reconcile()
        await self._persist(force=True)
        await self._write_ledger(inflight=False)
        self.ready = True
        await self.repo.add_event(
            "INFO",
            "ETH_PROCESS_START",
            f"ETH Grid 프로세스 시작 (pid {os.getpid()}, 상태 {self.status.value}, "
            f"{'실험 활성화됨' if self.enabled else '활성화 대기'})",
            {"run_id": self.run_id, "candidate": self.p.key},
        )
        if self._feed_factory is not None:
            self.feed = self._feed_factory(self.on_ws_message)
            self._tasks.append(asyncio.create_task(self.feed.run(), name="feed"))
        if run_loops:
            self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="supervisor"))

    async def stop(self) -> None:
        self._stop.set()
        if self.feed is not None:
            self.feed.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await self._persist(force=True)
            await self._write_ledger(inflight=self.engine.s.pending is not None)
            await self.repo.add_event("INFO", "ETH_PROCESS_STOP", "ETH Grid 프로세스 종료", {})

    async def _restore(self) -> None:
        saved = await self.repo.get_state(STATE_KEY)
        if saved and "cash" in saved:
            st = LiveGridState.from_dict(saved)
            self.engine = LiveGridEngine(self.p, st, policy=self.policy)
            log.warning(
                "eth_grid_state_restored",
                cash=str(st.cash),
                lots=len(st.lots),
                anchor=str(st.anchor),
                pending=st.pending.client_order_id if st.pending else None,
                frozen=st.frozen,
            )
        else:
            self.engine = LiveGridEngine(self.p, policy=self.policy)
            self.engine.s.started_at = self.started_at
        cyc = await self.repo.get_state(CYCLES_KEY) or {}
        from app.eth_grid.live_engine import Cycle

        self.engine.cycles = [Cycle.from_dict(c) for c in cyc.get("cycles", [])]
        self.engine.s.completed_cycles = max(
            self.engine.s.completed_cycles, len(self.engine.cycles)
        )
        if self.engine.s.anchor == 0:
            self.engine.adopt_rules(
                self.target_epoch, self.target_gap
            )  # fresh start: seed under rules
        if (
            self.engine.s.strategy_epoch != self.target_epoch
            or self.engine.s.gap != self.target_gap
        ):
            log.warning(
                "eth_grid_cutover_pending",
                from_epoch=self.engine.s.strategy_epoch,
                to_epoch=self.target_epoch,
                to_gap=str(self.target_gap),
                note="applies at the first real tick; no synthetic buys",
            )
        prev_unit = self.engine.current_unit
        unit = self.engine.update_sizing(datetime.now(UTC), "STARTUP")
        log.warning(
            "eth_grid_compounding",
            enabled=self.policy.enabled,
            capital=str(self.engine.realized_capital()),
            unit=str(unit),
            prev_unit=str(prev_unit),
            epoch=self.engine.s.sizing_epoch,
            paused=self.engine.s.sizing_paused_reason,
        )
        self.reconcile_last = dict(saved.get("reconcile", {})) if saved else {}
        await self.orders.load()

    async def _recover_pending(self) -> None:
        """Resolve an order that was in flight when the process died (write-ahead `pending`)."""
        now = datetime.now(UTC)
        recovered = await self.orders.recover_unknown()
        recovered += await self.orders.resolve_inflight()
        pend = self.engine.s.pending
        if pend is None:
            if recovered:
                log.warning("eth_grid_orphan_orders_resolved", n=len(recovered))
            return
        rec = next((r for r in recovered if r.client_order_id == pend.client_order_id), None)
        if rec is None:
            rec = await self.repo.get_order(pend.client_order_id)
        if rec is None:
            # the process died before the order row was written: ask the exchange directly
            try:
                gw = await self.gateway.get_order(client_order_id=pend.client_order_id)
            except OrderNotFoundError:
                self.engine.clear_pending(now, "never_reached_exchange")
                log.warning("eth_grid_pending_never_reached_exchange", cid=pend.client_order_id)
                return
            except GatewayNetworkError as exc:
                self.engine.freeze(now, f"RECOVERY_LOOKUP_FAILED {exc}")
                return
            rec = OrderRecord(
                client_order_id=pend.client_order_id,
                mode=TradingMode.LIVE,
                market=self.market,
                side=Side(pend.side),
                ord_type=OrdType.PRICE if pend.side == "bid" else OrdType.MARKET,
                state=OrderState.SUBMITTED,
                created_at=pend.created_at,
                updated_at=now,
                req_krw=pend.krw,
                req_volume=pend.quantity,
                submitted_at=pend.created_at,
            )
            self.orders._apply_gateway(rec, gw)
            if not rec.is_terminal:
                await self.orders._poll_until_terminal(rec)
            await self.repo.save_order(rec)
        if not rec.is_terminal:
            self.engine.freeze(now, f"RECOVERY_UNRESOLVED {rec.state.value}")
            return
        log.warning(
            "eth_grid_pending_recovered",
            cid=rec.client_order_id,
            state=rec.state.value,
            executed=str(rec.executed_volume),
        )
        await self._settle(rec, now, recovered=True)

    async def _bootstrap_trend(self) -> None:
        try:
            n = await asyncio.to_thread(self.trend.load_db, self.cfg.eth_market_db)
            log.info("eth_grid_trend_db_loaded", days=n)
        except Exception as exc:
            log.error("eth_grid_trend_db_failed", error=repr(exc))
        await self._refresh_trend(datetime.now(UTC), force=True)

    async def _refresh_trend(self, now: datetime, *, force: bool = False) -> None:
        if (
            not force
            and self.last_trend_at is not None
            and (now - self.last_trend_at).total_seconds() < self.cfg.trend_refresh_sec
        ):
            return
        self.last_trend_at = now
        if self.rest is not None:
            try:
                rows = await self.rest.get_minute_candles(self.market, 240, count=200)
                self.trend.add_4h_candles([(c.open_time, c.trade_price) for c in rows], now)
            except Exception as exc:
                self.errors += 1
                self.last_error = f"trend_refresh: {exc!r}"
                log.warning("eth_grid_trend_refresh_failed", error=repr(exc))
        r = self.trend.evaluate()
        if r.daily_close is not None:
            self.engine.set_trend(now, r.daily_close, r.ema)
            await self._flush_events()
        log.info(
            "eth_grid_trend",
            ok=r.ok,
            last_day=str(r.last_day),
            close=str(r.daily_close),
            ema=str(r.ema),
            days=r.days,
        )

    async def _preflight(self) -> None:
        now = datetime.now(UTC)
        try:
            await self._refresh_balances()
            open_ex = await self.gateway.get_open_orders()
        except GatewayNetworkError as exc:
            self.engine.freeze(now, f"PREFLIGHT_EXCHANGE_UNREACHABLE {exc}")
            return
        ours = {o.client_order_id for o in self.orders.inflight()}
        foreign = [o.client_order_id for o in open_ex if o.client_order_id not in ours]
        if foreign:
            self.engine.freeze(now, f"FOREIGN_OPEN_ORDERS {foreign[:3]}")
        # the D0 book must already have handed this sleeve its cash (trader carve-out)
        transfers = await self.main_repo.get_state("sleeve_transfers") or {}
        t = transfers.get(LEDGER_ID) or {}
        self.carve_out_done = t.get("status") == "DONE"
        if not self.carve_out_done:
            log.warning("eth_grid_carve_out_pending", transfer=t)

    # ------------------------------------------------------------------ market data
    async def on_ws_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "ticker":
            return
        raw = msg.get("trade_timestamp") or msg.get("timestamp")
        ts = (
            datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
            if isinstance(raw, int | float)
            else datetime.now(UTC)
        )
        await self.on_tick(Decimal(str(msg["trade_price"])), ts)

    async def on_tick(self, price: Decimal, ts: datetime) -> None:
        self.ticks += 1
        self.last_price = price
        if self.last_data_at is None or ts > self.last_data_at:
            self.last_data_at = ts
        if not self.ready:
            return
        eng = self.engine
        if eng.s.strategy_epoch != self.target_epoch or eng.s.gap != self.target_gap:
            # LIVE strategy cutover at the current REAL price; this tick creates no order
            if eng.cutover(ts, price, self.target_epoch, self.target_gap, self.legacy_label):
                log.warning(
                    "eth_grid_cutover",
                    to_epoch=self.target_epoch,
                    gap=str(self.target_gap),
                    activation_price=str(price),
                    legacy_lots=len([lot for lot in eng.s.lots if lot.exit_only]),
                )
                eng.end_observation(ts, price)
                await self._flush_events()
                await self._persist(force=True)
                await self._write_ledger(inflight=False)
                return
        skip_lots: set[int] = set()
        skip_levels: set[int] = set()
        executed = False
        for _ in range(MAX_INTENTS_PER_OBSERVATION):
            intent = eng.next_intent(
                ts,
                price,
                enabled=self.enabled and getattr(self, "carve_out_done", False),
                skip_lots=frozenset(skip_lots),
                skip_levels=frozenset(skip_levels),
            )
            if intent is None:
                break
            ctx = await self._risk_context(price)
            decision = self.risk.evaluate(intent, eng, ctx)
            if not decision.approved:
                await self.repo.add_event(
                    "WARN" if decision.reason in GridRisk.FREEZE_REASONS else "INFO",
                    "ETH_RISK_REJECTED",
                    f"{intent.kind} {intent.reason} 보류: {decision.reason}",
                    {"intent": intent.kind, "reason": decision.reason, **decision.snapshot},
                )
                if decision.reason in GridRisk.FREEZE_REASONS:
                    eng.freeze(ts, str(decision.reason))
                    break
                if intent.kind == "SEED":
                    break
                if intent.lot_id is not None:
                    skip_lots.add(intent.lot_id)
                if intent.level is not None:
                    skip_levels.add(intent.level)
                continue
            await self._execute(intent, ts)
            executed = True
            if eng.s.frozen or eng.s.pending is not None:
                break
        eng.end_observation(ts, price)
        await self._flush_events()
        if executed:
            await self._persist(force=True)

    async def _risk_context(self, price: Decimal) -> GridRiskContext:
        now = datetime.now(UTC)
        day_start = (
            now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        )
        return GridRiskContext(
            now=now,
            price=price,
            data_age_sec=self.data_age(now),
            inflight_orders=len(self.orders.inflight()),
            unknown_orders=len(self.orders.unknown()),
            orders_last_hour=await self.repo.count_orders_between(
                TradingMode.LIVE, now - timedelta(hours=1), market=self.market
            ),
            orders_today=await self.repo.count_orders_between(
                TradingMode.LIVE, day_start, market=self.market
            ),
            exchange_krw=self.bal.get("KRW"),
            exchange_eth=self.bal.get("ETH"),
            min_total=self.min_total,
            fee_rate=self.fee_rate,
        )

    # ------------------------------------------------------------------ execution
    async def _execute(self, intent: Intent, ts: datetime) -> None:
        eng = self.engine
        seq = eng.s.order_seq + 1
        tag = (
            "seed"
            if intent.kind == "SEED"
            else f"L{intent.level:+d}"
            if intent.kind == "GRID_BUY"
            else f"lot{intent.lot_id}"
        )
        cid = f"ethg-{self.epoch}-{seq:05d}-{'B' if intent.side is Side.BID else 'S'}-{tag}"[:64]
        eng.mark_pending(intent, cid, ts)
        # write-ahead: state (pending) + ledger in-flight BEFORE the exchange call
        try:
            await self._persist(force=True)
            await self._write_ledger(inflight=True, strict=True)
        except Exception as exc:
            eng.clear_pending(ts, f"write_ahead_failed {exc!r}")
            eng.freeze(ts, "WRITE_AHEAD_FAILED")
            with contextlib.suppress(Exception):
                await self._persist(force=True)
            return
        oi = OrderIntent(
            client_order_id=cid,
            market=self.market,
            side=intent.side,
            ord_type=OrdType.PRICE if intent.side is Side.BID else OrdType.MARKET,
            krw_amount=intent.krw.quantize(Decimal(1)) if intent.krw is not None else None,
            volume=intent.quantity,
            signal_id=None,
            mode=TradingMode.LIVE,
        )
        try:
            rec = await self.orders.submit(oi)
        except OrderBlockedError as exc:
            eng.clear_pending(ts, exc.reason)
            await self._persist(force=True)
            await self._write_ledger(inflight=False)
            return
        await self._settle(rec, ts)

    async def _settle(self, rec: OrderRecord, ts: datetime, *, recovered: bool = False) -> None:
        eng = self.engine
        if rec.state is OrderState.UNKNOWN:
            eng.freeze(ts, "ORDER_UNKNOWN_UNRESOLVED")  # pending kept: resolved at next start
        elif rec.state is OrderState.REJECTED or rec.executed_volume <= 0:
            eng.clear_pending(ts, f"{rec.state.value} {rec.error_code or ''}".strip())
            self.consecutive_rejects += 1
            if self.consecutive_rejects >= 3:
                eng.freeze(ts, "ORDER_REJECTED_REPEATEDLY")
        else:
            self.consecutive_rejects = 0
            fill_ts = rec.fills[-1].timestamp if rec.fills else ts
            if rec.side is Side.BID:
                eng.apply_buy(rec, fill_ts)
            else:
                eng.apply_sell(rec, fill_ts)
            if rec.state is OrderState.CANCELLED:
                log.warning("eth_grid_partial_fill_remainder_cancelled", cid=rec.client_order_id)
            rec.raw.update(
                {
                    "strategy_epoch": eng.s.strategy_epoch,
                    "candidate": self.p.key,
                    "sizing_epoch": eng.s.sizing_epoch,
                    "unit_krw": str(rec.req_krw) if rec.side is Side.BID else None,
                }
            )
            with contextlib.suppress(Exception):
                await self.repo.save_order(rec)
        await self._persist(force=True)
        await self._write_ledger(inflight=eng.s.pending is not None)
        with contextlib.suppress(Exception):
            await self._refresh_balances()
        if self.orders.consecutive_api_errors >= self.cfg.api_error_consecutive:
            eng.freeze(ts, "API_ERRORS")
        await self._flush_events()
        if recovered:
            await self.repo.add_event(
                "WARN",
                "ETH_RECOVERY",
                f"재시작 복구: 주문 {rec.client_order_id} {rec.state.value} 반영",
                {"client_order_id": rec.client_order_id, "state": rec.state.value},
            )

    # ------------------------------------------------------------------ reconciliation
    async def _refresh_balances(self) -> None:
        self.bal = await self.balances()
        self.bal_at = datetime.now(UTC)

    async def _d0_ledger(self) -> tuple[SleeveLedger, bool]:
        pf = await self.main_repo.get_state("portfolio") or {}
        st = pf.get("state") or {}
        lg_raw = await self.main_repo.get_state("sleeve_ledger_BTC_D0") or {}
        inflight = bool(lg_raw.get("inflight", False))
        return (
            SleeveLedger(
                "BTC_D0",
                "BTC",
                Decimal(str(st.get("krw", "0"))),
                Decimal(str(st.get("btc", "0"))),
                mode="LIVE",
                owner="trader",
                inflight=inflight,
                updated_at=str(lg_raw.get("updated_at", "")),
            ),
            inflight,
        )

    def _own_ledger(self, inflight: bool) -> SleeveLedger:
        s = self.engine.s
        return SleeveLedger(
            LEDGER_ID,
            "ETH",
            s.cash,
            s.inventory_qty,
            mode="LIVE_EXPERIMENT",
            owner="eth_grid",
            inflight=inflight,
            updated_at=datetime.now(UTC).isoformat(),
            last_trade_at=s.last_fill_at.isoformat() if s.last_fill_at else "",
        )

    async def _reconcile(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        self.last_reconcile_at = now
        tol = {
            "krw_tolerance": self.ks.reconcile_krw_tolerance,
            "coin_tolerance": self.ks.reconcile_btc_tolerance,
        }
        try:
            await self._refresh_balances()
            d0, d0_inflight = await self._d0_ledger()
            res = reconcile_multi_asset(self.bal, [d0, self._own_ledger(False)], **tol)
            open_ex = await self.gateway.get_open_orders()
            ours = {o.client_order_id for o in self.orders.inflight()}
            foreign = sorted(o.client_order_id for o in open_ex if o.client_order_id not in ours)
            if not res.ok or foreign:
                await asyncio.sleep(RECHECK_DELAY_SEC)
                await self._refresh_balances()
                d0, d0_inflight = await self._d0_ledger()
                res = reconcile_multi_asset(self.bal, [d0, self._own_ledger(False)], **tol)
                open_ex = await self.gateway.get_open_orders()
                foreign = sorted(
                    o.client_order_id for o in open_ex if o.client_order_id not in ours
                )
        except GatewayNetworkError as exc:
            self.errors += 1
            self.last_error = f"reconcile: {exc!r}"
            self.reconcile_last = {"status": "ERROR", "at": now.isoformat(), "detail": repr(exc)}
            return self.reconcile_last
        ok = res.ok and not foreign
        status = "OK" if ok else ("WARN" if d0_inflight else "FAIL")
        out: dict[str, Any] = {
            "status": status,
            "at": now.isoformat(),
            "exchange": {k: str(v) for k, v in res.exchange.items()},
            "expected": {k: str(v) for k, v in res.expected.items()},
            "diffs": {k: str(v) for k, v in res.diffs.items()},
            "detail": res.detail + (f" foreign open orders {foreign}" if foreign else ""),
            "d0_inflight": d0_inflight,
            "foreign_open_orders": foreign,
        }
        self.reconcile_last = out
        if ok:
            self.reconcile_fails = 0
        elif status == "FAIL":
            self.reconcile_fails += 1
            await self.repo.add_event(
                "ERROR",
                "ETH_RECONCILE_WARN",
                f"잔고 대조 불일치 ({self.reconcile_fails}/{self.cfg.reconcile_fail_limit}): "
                f"{out['detail']}",
                out,
            )
            if self.reconcile_fails >= self.cfg.reconcile_fail_limit:
                self.engine.freeze(now, f"RECONCILE_FAILED {out['detail']}")
                await self._flush_events()
                await self._persist(force=True)
                with contextlib.suppress(Exception):
                    await self._write_ledger(inflight=self.engine.s.pending is not None)
        else:
            await self.repo.add_event(
                "WARN",
                "ETH_RECONCILE_DEFERRED",
                f"잔고 대조 보류 (BTC 전략 주문 처리 중): {out['detail']}",
                out,
            )
        return out

    # ------------------------------------------------------------------ persistence
    async def _write_ledger(self, *, inflight: bool, strict: bool = False) -> None:
        lg = self._own_ledger(inflight)
        px = self.last_price or self.engine.s.last_price
        doc = {
            **lg.to_dict(),
            "status": self.status.value,
            "equity": str(self.engine.s.equity(px)) if px else str(self.engine.s.cash),
            "last_price": str(px) if px else None,
            "pid": os.getpid(),
            "run_id": self.run_id,
        }
        try:
            await self.main_repo.set_state(lg.key(), doc)
            self._main_db_errors = 0
        except Exception as exc:
            self._main_db_errors += 1
            self.errors += 1
            self.last_error = f"ledger_write: {exc!r}"
            log.error("eth_grid_ledger_write_failed", error=repr(exc), n=self._main_db_errors)
            if strict or self._main_db_errors >= self.cfg.db_write_error_limit:
                raise

    def _health(self) -> dict[str, Any]:
        ws = self.feed.health if self.feed is not None else None
        return {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "heartbeat": (self.last_heartbeat or datetime.now(UTC)).isoformat(),
            "last_tick_at": self.last_data_at.isoformat() if self.last_data_at else None,
            "last_price": str(self.last_price) if self.last_price is not None else None,
            "ticks": self.ticks,
            "ws_connected": bool(ws.connected) if ws else False,
            "reconnects": ws.reconnect_count if ws else 0,
            "errors": self.errors,
            "last_error": self.last_error,
            "api_errors": self.orders.consecutive_api_errors,
            "db_write_errors": self.repo.write_errors,
            "enabled": self.enabled,
            "enabled_at": self.enabled_at,
            "ready": self.ready,
            "carve_out_done": getattr(self, "carve_out_done", False),
            "balances": {k: str(v) for k, v in self.bal.items()},
            "balances_at": self.bal_at.isoformat() if self.bal_at else None,
            "trend": {
                "ok": self.trend.last.ok,
                "last_day": str(self.trend.last.last_day),
                "close": str(self.trend.last.daily_close),
                "ema": str(self.trend.last.ema),
                "days": self.trend.last.days,
            },
        }

    async def _persist(self, *, force: bool = False) -> None:
        eng = self.engine
        doc = {
            **eng.s.to_dict(),
            "candidate": self.p.key,
            "params": self.p.to_dict(),
            "status": self.status.value,
            "health": self._health(),
            "reconcile": self.reconcile_last,
            "min_total": str(self.min_total),
            "fee_rate": str(self.fee_rate),
            "buy_pause": {
                "paused": bool(eng.s.buy_pause_reason),
                "reason": eng.s.buy_pause_reason,
                "at": eng.s.buy_pause_at.isoformat() if eng.s.buy_pause_at else None,
                "by": eng.s.buy_pause_by,
            },
            "active_strategy": {
                "epoch": eng.s.strategy_epoch,
                "gap_pct": str(eng.s.gap),
                "target_epoch": self.target_epoch,
                "target_gap_pct": str(self.target_gap),
                "legacy_label": self.legacy_label,
                "cutover_pending": eng.s.strategy_epoch != self.target_epoch,
            },
            "compounding_policy": self.policy.to_dict(),
            "compounding": eng.compounding_view(self.last_price or eng.s.last_price),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self.repo.set_state(STATE_KEY, doc)
        if force or len(eng.cycles) != int(
            (await self.repo.get_state(CYCLES_KEY) or {}).get("n", -1)
        ):
            await self.repo.set_state(
                CYCLES_KEY, {"n": len(eng.cycles), "cycles": [c.to_dict() for c in eng.cycles]}
            )

    async def _flush_events(self) -> None:
        for ev in self.engine.drain_events():
            typ, level, msg = korean_event(ev)
            with contextlib.suppress(Exception):
                await self.repo.add_event(level, typ, msg, {"ts": ev.ts.isoformat(), **ev.detail})
            log.warning("eth_grid_event", kind=ev.kind, **ev.detail)

    async def _persist_bus_event(self, ev: Event) -> None:
        if ev.type in (EventType.ORDER_CREATED, EventType.ORDER_SUBMITTED):
            return  # order lifecycle noise; fills/rejections are reported as Korean grid events
        with contextlib.suppress(Exception):
            await self.repo.add_event(ev.level.value, ev.type.value, ev.message, ev.payload)

    # ------------------------------------------------------------------ control / supervisor
    async def _poll_control(self, *, force: bool = False) -> None:
        now = datetime.now(UTC)
        if not force and self.last_control_at and (now - self.last_control_at).total_seconds() < 5:
            return
        self.last_control_at = now
        ctl = await self.repo.get_state(CONTROL_KEY) or {}
        enabled = bool(ctl.get("enabled", False))
        if enabled and not self.enabled:
            self.enabled, self.enabled_at = True, str(ctl.get("enabled_at") or now.isoformat())
            await self.repo.add_event(
                "WARN",
                "ETH_LIVE_EXPERIMENT_ENABLED",
                "ETH LIVE EXPERIMENT 시작 — ₩50,000 한도 실제 돈 실험 (연구 Gate 미통과 전략)",
                {"enabled_by": ctl.get("enabled_by"), "enabled_at": self.enabled_at},
            )
        elif not enabled and self.enabled:
            self.enabled = False
            await self.repo.add_event(
                "WARN",
                "ETH_LIVE_EXPERIMENT_DISABLED",
                "ETH LIVE EXPERIMENT 비활성화 — 신규 주문 없음",
                {},
            )
        pause_reason = (
            str(ctl.get("buy_pause_reason") or "PAUSED_NEW_BUYS") if ctl.get("buy_paused") else None
        )
        if self.engine.set_buy_pause(now, pause_reason, str(ctl.get("buy_pause_by") or "operator")):
            await self._flush_events()
            await self._persist(force=True)
        req = ctl.get("unfreeze_request_at")
        if req and self.engine.s.frozen and self.engine.s.frozen_at is not None:
            try:
                if datetime.fromisoformat(str(req)) > self.engine.s.frozen_at:
                    self.engine.unfreeze(now, str(ctl.get("unfreeze_by") or "operator"))
                    self.reconcile_fails = 0
                    self.consecutive_rejects = 0
                    await self._flush_events()
            except ValueError:
                pass

    async def supervisor_tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.last_heartbeat = now
        await self._poll_control()
        if self.repo.write_errors - self._db_errors_seen >= self.cfg.db_write_error_limit:
            self._db_errors_seen = self.repo.write_errors
            self.engine.freeze(now, "DATABASE_FAILURE")
        if self.orders.consecutive_api_errors >= self.cfg.api_error_consecutive:
            self.engine.freeze(now, "API_ERRORS")
        if self.ready and (
            self.last_reconcile_at is None
            or (now - self.last_reconcile_at).total_seconds() >= self.cfg.reconcile_interval_sec
        ):
            if not self.orders.inflight():
                await self._reconcile()
        await self._refresh_trend(now)
        await self._flush_events()
        await self._persist()
        if self.ready:
            await self._write_ledger(inflight=self.engine.s.pending is not None)

    async def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.supervisor_tick()
            except Exception as exc:
                self.errors += 1
                self.last_error = f"supervisor: {exc!r}"
                log.error("eth_grid_supervisor_error", error=repr(exc))
            await asyncio.sleep(self.cfg.heartbeat_sec)


__all__ = ["CONTROL_KEY", "CYCLES_KEY", "STATE_KEY", "EthGridService", "korean_event"]
