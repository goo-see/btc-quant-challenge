"""Deterministic replay / synthetic E2E scenarios for Strategy D on the real PAPER pipeline.

Each scenario builds a TraderService (PAPER, PaperGateway, fake feed, temp SQLite), replays a
synthetic 1m price path through the SAME code path live data takes (ticker -> stop check,
candle.1m -> aggregator -> indicators -> strategy -> risk -> order -> portfolio -> reconcile)
and asserts the expected outcome. Used by pytest and by scripts/replay_e2e.py (which records
the summary into bot_state.replay_e2e as readiness evidence).

Most scenarios run trend_following_4h's class on 15m/1h buckets for speed (the strategy logic
is timeframe-agnostic); two scenarios use the real 4h/1d buckets.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.settings import AppConfig, EnvSettings, Settings, load_yaml_config
from app.core.state import BotState
from app.core.types import Action, Candle, OrderState, Signal, Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.ws_client import WsHealth
from app.execution.fill_model import FillParams
from app.execution.paper_gateway import PaperGateway
from app.monitoring.health import ProbeResult
from app.services.trader_service import TraderService

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Monday 00:00 UTC (4h/1d aligned)
P0 = 100_000_000.0

FAST_PARAMS: dict[str, float] = {
    "entry_n": 6,
    "exit_n": 3,
    "trend_ema": 8,
    "atr_period": 5,
    "atr_stop_mult": 2.0,
    "atr_trail_mult": 3.0,
    "exit_on_trend_loss": 1,
    "rsi_period": 5,
    "volume_sma_period": 5,
    "ema_fast": 5,
    "ema_slow": 8,
}


class FakeFeed:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.health = WsHealth(connected=True)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


class Probe:
    def __init__(self) -> None:
        self.result = ProbeResult(rest_ok=True, exchange_trade_age_sec=1.0)

    async def __call__(self) -> ProbeResult:
        return self.result


# ------------------------------------------------------------------ synthetic path builder
@dataclass(slots=True)
class Path1m:
    candles: list[Candle] = field(default_factory=list)
    price: float = P0
    t: datetime = T0

    def _bar(self, o: float, c: float, wiggle: float = 0.0004) -> Candle:
        hi = max(o, c) * (1 + wiggle)
        lo = min(o, c) * (1 - wiggle)
        cd = Candle(
            "KRW-BTC",
            Timeframe.M1,
            self.t,
            Decimal(round(o)),
            Decimal(round(hi)),
            Decimal(round(lo)),
            Decimal(round(c)),
            Decimal("0.5"),
            Decimal(round(c * 0.5)),
        )
        self.t += timedelta(minutes=1)
        self.price = c
        return cd

    def flat(self, minutes: int, wobble: float = 0.0002) -> Path1m:
        for i in range(minutes):
            o = self.price
            c = o * (1 + (wobble if i % 2 == 0 else -wobble))
            self.candles.append(self._bar(o, c))
        return self

    def move(self, minutes: int, total_pct: float) -> Path1m:
        step = (1 + total_pct) ** (1 / max(minutes, 1)) - 1
        for _ in range(minutes):
            o = self.price
            self.candles.append(self._bar(o, o * (1 + step)))
        return self

    def gap(self, pct: float) -> Path1m:
        """Next bar opens `pct` away from the last close (no trades in between)."""
        o = self.price * (1 + pct)
        self.candles.append(self._bar(o, o))
        return self


def tick(price: float, ts: datetime) -> dict[str, Any]:
    ms = int(ts.timestamp() * 1000)
    return {
        "type": "ticker",
        "code": "KRW-BTC",
        "trade_price": price,
        "trade_timestamp": ms,
        "timestamp": ms,
    }


def candle_msg(c: Candle) -> dict[str, Any]:
    return {
        "type": "candle.1m",
        "code": "KRW-BTC",
        "candle_date_time_utc": c.open_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "opening_price": float(c.open),
        "high_price": float(c.high),
        "low_price": float(c.low),
        "trade_price": float(c.close),
        "candle_acc_trade_volume": float(c.volume),
        "candle_acc_trade_price": float(c.quote_volume),
        "timestamp": int(c.open_time.timestamp() * 1000),
    }


# ------------------------------------------------------------------ harness
@dataclass(slots=True)
class World:
    svc: TraderService
    repo: Repository
    gw: PaperGateway
    probe: Probe
    settings: Settings
    tmp: Path
    now: datetime = T0

    async def feed(self, candles: list[Candle], *, ticks: str = "low,close") -> None:
        """Replay closed 1m candles: intrabar ticks first (STOP-first: low before high), then the bar."""
        for c in candles:
            order = ticks.split(",")
            for kind in order:
                px = {
                    "low": float(c.low),
                    "high": float(c.high),
                    "close": float(c.close),
                    "open": float(c.open),
                }[kind]
                await self.svc.on_ws_message(tick(px, c.open_time + timedelta(seconds=10)))
            await self.svc.on_ws_message(candle_msg(c))
            self.now = c.open_time + timedelta(minutes=1)
            if (c.open_time.minute % 5) == 0:
                await self.svc.supervisor_tick(self.now)

    async def supervise(self, seconds: float = 5) -> None:
        self.now += timedelta(seconds=seconds)
        await self.svc.supervisor_tick(self.now)

    async def close(self) -> None:
        await self.svc.stop()
        with contextlib.suppress(Exception):
            await self.repo._engine.dispose()


def _config(
    tmp: Path,
    *,
    tf: str = "15m",
    trend: str = "1h",
    params: dict[str, float] | None = None,
    **risk: Any,
) -> Settings:
    root = Path(__file__).resolve().parents[3]
    cfg: AppConfig = load_yaml_config(root / "config" / "config.yaml")
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp}/replay.db"
    cfg.system.warmup_days = 3
    cfg.system.warmup_days_1h = 30
    cfg.system.reconcile_interval_sec = 0
    cfg.strategy.version_file = None
    cfg.strategy.name = "trend_following_4h"
    cfg.strategy.version = "trend_following_4h_v1-replay"
    cfg.strategy.timeframe = tf
    cfg.strategy.trend_timeframe = trend
    cfg.strategy.params = dict(params or FAST_PARAMS)
    cfg.risk.cooldown_after_loss_min = int(risk.get("cooldown", 0))
    cfg.risk.max_orders_per_hour = int(risk.get("orders_per_hour", 1000))
    cfg.risk.max_consecutive_losses = 1000
    cfg.kill_switch.market_data_stale_sec = 120
    env = EnvSettings(_env_file=None, TRADING_MODE="PAPER")
    return Settings(env, cfg)


async def make_world(
    tmp: Path,
    *,
    initial_krw: Decimal = Decimal(100000),
    run_id: str = "replay",
    seed_1h: list[Candle] | None = None,
    **kw: Any,
) -> World:
    settings = _config(tmp, **kw)
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    if seed_1h:
        await repo.upsert_candles(seed_1h, source="rest")
    gw = PaperGateway(
        market="KRW-BTC",
        initial_krw=initial_krw,
        fill_params=FillParams(Decimal("0.0005"), Decimal(3)),
        min_order_krw=Decimal(5000),
    )
    probe = Probe()
    svc = TraderService(settings, repo, gw, feed_factory=FakeFeed, probe=probe, run_id=run_id)
    await svc.start(run_loops=False)
    return World(svc, repo, gw, probe, settings, tmp)


async def reopen_world(w: World, *, run_id: str = "replay-2") -> World:
    """Simulate a process restart on the same DB (fresh gateway object restored from bot_state)."""
    await w.svc.stop()
    settings = w.settings
    engine = create_engine(settings.config.system.database_url)
    repo = Repository(engine)
    gw = PaperGateway(
        market="KRW-BTC",
        initial_krw=Decimal(100000),
        fill_params=FillParams(Decimal("0.0005"), Decimal(3)),
        min_order_krw=Decimal(5000),
    )
    probe = Probe()
    svc = TraderService(settings, repo, gw, feed_factory=FakeFeed, probe=probe, run_id=run_id)
    await svc.start(run_loops=False)
    return World(svc, repo, gw, probe, settings, w.tmp, w.now)


def warm_path(bars_15m: int = 40) -> Path1m:
    """Enough flat-ish history for 15m/1h indicators (EMA8 on 1h needs 8h = 480 min)."""
    return Path1m().flat(bars_15m * 15)


async def bring_online(w: World, p: Path1m) -> None:
    await w.feed(p.candles)
    await w.supervise()
    assert _st(w) is BotState.RUNNING, w.svc.recovery.last


async def breakout(w: World, *, pct: float = 0.02, minutes: int = 30) -> Path1m:
    """A clean upward move that closes above the channel high on the strategy timeframe."""
    p = Path1m(price=(w.svc.portfolio.price and float(w.svc.portfolio.price)) or P0, t=w.now)
    p.move(minutes, pct)
    await w.feed(p.candles)
    return p


# ------------------------------------------------------------------ scenarios
@dataclass(slots=True)
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ""


Scenario = Callable[[Path], Awaitable[str]]
SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str) -> Callable[[Scenario], Scenario]:
    def deco(fn: Scenario) -> Scenario:
        SCENARIOS[name] = fn
        return fn

    return deco


async def _entered(w: World, legs: int = 6) -> None:
    for _ in range(legs):  # breakout legs until the channel breaks (channel needs entry_n bars)
        if w.svc.portfolio.state.has_position:
            return
        await breakout(w)
    assert w.svc.portfolio.state.has_position, "no entry after breakout legs"


async def _buy_signals(w: World) -> int:
    return sum(1 for s in await w.repo.recent_signals(1000) if s.action == "BUY")


async def _until_buy_signal(w: World, legs: int = 6, pct: float = 0.03) -> int:
    """Feed breakout legs until the strategy emits at least one BUY signal (regardless of risk)."""
    for _ in range(legs):
        if await _buy_signals(w) > 0:
            break
        await breakout(w, pct=pct)
    n = await _buy_signals(w)
    assert n > 0, "strategy never emitted a BUY signal"
    return n


def _st(w: World) -> BotState:
    return w.svc.sm.state


def _orders(w: World) -> list[Any]:
    return [o for o in w.svc.orders._orders.values()]


@scenario("01_warmup_hold_only")
async def s01(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    sigs = await w.repo.recent_signals(500)
    try:
        assert sigs and all(s.action == "HOLD" for s in sigs)
        assert not _orders(w) and not w.svc.portfolio.state.has_position
        return f"{len(sigs)} HOLD signals, 0 orders"
    finally:
        await w.close()


@scenario("02_breakout_buy_risk_order_fill_position")
async def s02(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    o = _orders(w)
    ev = [e.type for e in await w.repo.recent_events(200)]
    try:
        assert len(o) == 1 and o[0].state is OrderState.FILLED and o[0].req_krw == Decimal(30000)
        assert {
            "SIGNAL_GENERATED",
            "RISK_APPROVED",
            "ORDER_CREATED",
            "ORDER_SUBMITTED",
            "ORDER_FILLED",
            "POSITION_OPENED",
        } <= set(ev)
        pf = w.svc.portfolio
        assert pf.state.btc > 0 and pf.stop_price is not None and pf.state.avg_entry is not None
        assert pf.stop_price < pf.state.avg_entry
        assert pf.open_trade_id is not None
        return f"filled {o[0].executed_volume} BTC @ {o[0].avg_fill_price:.0f}, stop {pf.stop_price:.0f}"
    finally:
        await w.close()


@scenario("03_no_second_buy_while_in_position")
async def s03(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    await breakout(w, pct=0.02)
    try:
        assert len(_orders(w)) == 1 and w.svc.orders.duplicates_blocked == 0
        buys = [s for s in await w.repo.recent_signals(500) if s.action == "BUY"]
        assert len(buys) == 1
        return "second breakout produced no BUY signal (strategy in position)"
    finally:
        await w.close()


@scenario("04_atr_stop_hit_via_ticker")
async def s04(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    stop = w.svc.portfolio.stop_price
    assert stop is not None
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert not w.svc.portfolio.state.has_position and len(tr) == 1
        assert str(tr[0].exit_reason).startswith("STOP@") and Decimal(str(tr[0].net_pnl)) < 0
        assert w.svc.portfolio.consecutive_losses == 1
        return f"stop {stop:.0f} hit -> exit {tr[0].exit_price:.0f}, pnl {tr[0].net_pnl:.0f}"
    finally:
        await w.close()


@scenario("05_trailing_stop_ratchets_up_only")
async def s05(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    s0 = w.svc.portfolio.stop_price
    assert s0 is not None
    seen = [s0]
    for _ in range(4):
        await breakout(w, pct=0.015, minutes=30)
        sp = w.svc.portfolio.stop_price
        assert sp is not None
        seen.append(sp)
    try:
        assert all(seen[i] >= seen[i - 1] for i in range(1, len(seen))) and seen[-1] > s0
        # pull back: trail should exit above the initial entry
        p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.05)
        await w.feed(p.candles)
        tr = await w.repo.closed_trades(TradingMode.PAPER)
        assert len(tr) == 1 and Decimal(str(tr[0].exit_price)) > Decimal(str(tr[0].entry_price))
        return f"stop ratcheted {s0:.0f} -> {seen[-1]:.0f}; trail exit pnl {tr[0].net_pnl:+.0f}"
    finally:
        await w.close()


@scenario("06_channel_exit_below_recent_low")
async def s06(tmp: Path) -> str:
    w = await make_world(tmp, params={**FAST_PARAMS, "atr_stop_mult": 8.0, "atr_trail_mult": 12.0})
    await bring_online(w, warm_path())
    await _entered(w)
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(
        15 * 6, -0.02
    )  # slow decline, 6 bars
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert len(tr) == 1 and str(tr[0].exit_reason).startswith("CHANNEL_EXIT")
        return f"exit {tr[0].exit_reason}"
    finally:
        await w.close()


@scenario("07_trend_loss_exit")
async def s07(tmp: Path) -> str:
    # channel exit disabled in practice (30-bar low sits at the flat warm-up level), stops far away:
    # a -4% drop then a 3h hold leaves close below the lagging 1h EMA8 -> TREND_LOSS_EXIT
    w = await make_world(
        tmp, params={**FAST_PARAMS, "exit_n": 60, "atr_stop_mult": 120.0, "atr_trail_mult": 160.0}
    )
    # 60 flat bars then a +10% ramp: the 60-bar low stays at the flat level far below later prices
    await bring_online(w, Path1m().flat(60 * 15).move(300, 0.10))
    await _entered(w)
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(60, -0.07).flat(240)
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert len(tr) == 1 and tr[0].exit_reason == "TREND_LOSS_EXIT", (
            tr[0].exit_reason if tr else "no trade"
        )
        return "trend loss exit"
    finally:
        await w.close()


@scenario("08_gap_down_through_stop")
async def s08(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    stop = w.svc.portfolio.stop_price
    assert stop is not None
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).gap(-0.10)
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert len(tr) == 1 and Decimal(str(tr[0].exit_price)) < stop * Decimal("0.95")
        return f"gap fill {tr[0].exit_price:.0f} << stop {stop:.0f} (no fill at stop price assumed)"
    finally:
        await w.close()


@scenario("09_stop_ambiguity_low_before_high")
async def s09(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    stop = w.svc.portfolio.stop_price
    assert stop is not None
    # one bar that spans both below the stop and far above: low tick is applied first -> STOP wins
    px = float(w.svc.portfolio.price)
    c = Candle(
        "KRW-BTC",
        Timeframe.M1,
        w.now,
        Decimal(round(px)),
        Decimal(round(px * 1.05)),
        Decimal(round(float(stop) * 0.99)),
        Decimal(round(px * 1.04)),
        Decimal(1),
        Decimal(1),
    )
    await w.feed([c], ticks="low,high,close")
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert (
            len(tr) == 1
            and str(tr[0].exit_reason).startswith("STOP@")
            and not w.svc.portfolio.state.has_position
        )
        return "STOP-first: same-bar low tick closed the position before the high tick"
    finally:
        await w.close()


@scenario("10_restart_with_open_position_restores_and_exits")
async def s10(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    pf = w.svc.portfolio
    snap = (pf.state.krw, pf.state.btc, pf.stop_price, pf.open_trade_id, w.gw.ledger.krw)
    last_px = float(pf.price)
    w2 = await reopen_world(w)
    pf2 = w2.svc.portfolio
    try:
        got = (pf2.state.krw, pf2.state.btc, pf2.stop_price, pf2.open_trade_id, w2.gw.ledger.krw)
        assert got == snap, f"restored {got} != {snap}"
        await w2.feed(Path1m(price=last_px, t=w2.now).flat(15).candles)
        await w2.supervise()
        assert _st(w2) is BotState.RUNNING
        p = Path1m(price=float(pf2.price), t=w2.now).move(20, -0.06)
        await w2.feed(p.candles)
        tr = await w2.repo.closed_trades(TradingMode.PAPER)
        assert len(tr) == 1 and not pf2.state.has_position
        assert (await w2.svc.reconciler.run([])).ok
        return "position/stop/ledger restored after restart; stop exit executed; reconcile OK"
    finally:
        await w2.close()


@scenario("11_reconciliation_ok_with_open_position")
async def s11(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    r = await w.svc.reconciler.run(w.svc.orders.inflight())
    try:
        assert r.ok and r.position_ok and r.balance_ok
        return f"krw_diff {r.krw_diff} btc_diff {r.btc_diff}"
    finally:
        await w.close()


@scenario("12_reconciliation_mismatch_halts_no_new_orders")
async def s12(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    w.gw.ledger.krw -= Decimal(5000)
    await w.supervise()
    try:
        assert _st(w) is BotState.HALTED and w.svc.sm.reason == "RECONCILE_FAILED"
        await breakout(w)
        await breakout(w)
        assert not _orders(w)
        rej = [e for e in await w.repo.recent_events(300) if e.type == "RISK_REJECTED"]
        assert any("KILL_SWITCH" in str(e.payload) for e in rej) or not rej
        return "HALTED on mismatch; breakouts produced no orders"
    finally:
        await w.close()


@scenario("13_duplicate_order_prevention")
async def s13(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    from app.core.types import OrderIntent, OrdType, Side

    intent = OrderIntent(
        "dup-1", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(10000), None, None, TradingMode.PAPER
    )
    a = await w.svc.orders.submit(intent)
    b = await w.svc.orders.submit(intent)
    try:
        assert (
            a.state is OrderState.FILLED
            and b is not None
            and w.gw.place_calls == 1
            and w.svc.orders.duplicates_blocked == 1
        )
        # and a second BUY signal while a position exists is rejected by risk (POSITION_STATE)
        sig = Signal(
            Action.BUY, 1.0, "FORCED", w.now, "x", Timeframe.M15, stop_price=Decimal(90_000_000)
        )
        await w.svc.portfolio.apply_order(a, reason="dup-test", stop=None)
        await w.svc.handle_signal(sig)
        rej = [e for e in await w.repo.recent_events(50) if e.type == "RISK_REJECTED"]
        assert rej and "POSITION_STATE" in str(rej[0].payload)
        return "same client_order_id -> 1 exchange call; BUY while long -> RISK_REJECTED"
    finally:
        await w.close()


@scenario("14_daily_loss_limit_rejects_and_halts")
async def s14(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    w.svc.portfolio.daily.start_equity = Decimal(104000)  # today's PnL now reads -4% (limit 3%)
    await _until_buy_signal(w)
    try:
        assert (
            _st(w) is BotState.HALTED and w.svc.sm.reason == "DAILY_LOSS_LIMIT" and not _orders(w)
        )
        return "DAILY_LOSS_LIMIT -> rejected + HALTED"
    finally:
        await w.close()


@scenario("15_paused_blocks_buy")
async def s15(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await w.repo.add_command("PAUSE", "test", confirmed=True, command_id="pause-1", ttl_sec=600)
    await w.svc.process_commands(now=datetime.now(UTC))
    await _until_buy_signal(w)
    rej = [e for e in await w.repo.recent_events(300) if e.type == "RISK_REJECTED"]
    try:
        assert _st(w) is BotState.PAUSED and not _orders(w)
        assert any("EXIT_ONLY_NO_NEW_ENTRY" in str(e.payload) for e in rej)
        return "BUY blocked while PAUSED (EXIT_ONLY_NO_NEW_ENTRY)"
    finally:
        await w.close()


@scenario("16_maintenance_blocks_then_recovers_and_trades")
async def s16(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    w.probe.result = ProbeResult(rest_ok=False, exchange_trade_age_sec=None, error="503")
    w.now += timedelta(seconds=200)
    await w.svc.supervisor_tick(w.now)
    try:
        assert _st(w) is BotState.MAINTENANCE
        w.probe.result = ProbeResult(rest_ok=True, exchange_trade_age_sec=1.0)
        await _entered_or_not(w)
        assert not _orders(w)  # no orders during MAINTENANCE
        await w.supervise()
        assert _st(w) is BotState.RUNNING
        await _entered(w)
        assert len(_orders(w)) == 1
        return "no orders in MAINTENANCE; auto-recovered; next breakout traded"
    finally:
        await w.close()


async def _entered_or_not(w: World) -> None:
    await breakout(w)


@scenario("17_kill_switch_keeps_position_no_auto_liquidation")
async def s17(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    await w.svc.kill.trigger("MANUAL_KILL", {})
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.08)
    await w.feed(p.candles)
    try:
        assert (
            _st(w) is BotState.HALTED
            and w.svc.portfolio.state.has_position
            and len(_orders(w)) == 1
        )
        return "HALTED with open position: stop breached but NO sell (trading disabled)"
    finally:
        await w.close()


@scenario("18_gateway_reject_path_then_recovers")
async def s18(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    w.gw.faults.next_place = "reject"  # exchange answers 400 market_offline for the next order
    await _until_buy_signal(w)
    o = _orders(w)
    ev = [e.type for e in await w.repo.recent_events(300)]
    try:
        assert o and o[0].state is OrderState.REJECTED and o[0].error_code == "market_offline"
        assert "ORDER_REJECTED" in ev
        # a rejected order leaves the books untouched; the strategy is still flat and may re-signal
        assert (await w.svc.reconciler.run([])).ok
        await _entered(w)  # the next valid breakout trades normally
        filled = [x for x in _orders(w) if x.state is OrderState.FILLED]
        assert len(filled) == 1 and w.svc.portfolio.state.btc == w.gw.ledger.btc
        return "gateway rejection recorded (REJECTED/market_offline), books consistent, next entry filled once"
    finally:
        await w.close()


@scenario("19_network_unknown_resolved_single_fill")
async def s19(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    w.gw.faults.next_place = "network_after_accept"
    await _entered(w)
    o = _orders(w)
    ev = [e.type for e in await w.repo.recent_events(300)]
    try:
        assert (
            len(o) == 1
            and o[0].state is OrderState.FILLED
            and "ORDER_UNKNOWN" in ev
            and w.gw.place_calls == 1
        )
        assert w.svc.portfolio.state.btc == w.gw.ledger.btc
        return (
            "UNKNOWN after network error -> resolved FILLED by identifier lookup, exactly one fill"
        )
    finally:
        await w.close()


@scenario("20_min_order_size_reject_small_equity")
async def s20(tmp: Path) -> str:
    w = await make_world(tmp, initial_krw=Decimal(12000))
    # a new strategy epoch resets PAPER books to the configured principal; re-apply the small account
    w.gw.ledger.krw = Decimal(12000)
    w.svc.portfolio.state.krw = Decimal(12000)
    w.svc.portfolio.state.initial_equity = Decimal(12000)
    w.svc.portfolio.state.hwm = Decimal(12000)
    w.svc.portfolio.daily.start_equity = Decimal(12000)
    await bring_online(w, warm_path())
    await _until_buy_signal(w)
    rej = [e for e in await w.repo.recent_events(300) if e.type == "RISK_REJECTED"]
    try:
        assert any("MIN_ORDER" in str(e.payload) for e in rej) and not _orders(w)
        return "30% of ₩12,000 = ₩3,600 < ₩5,000 -> MIN_ORDER"
    finally:
        await w.close()


@scenario("21_partial_sell_fill_then_cancel_stays_consistent")
async def s21(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    w.gw.faults.next_place = "partial"
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)
    await w.feed(p.candles)
    o = _orders(w)
    try:
        sells = [x for x in o if x.side.value == "ask"]
        assert sells and sells[0].state is OrderState.CANCELLED and sells[0].executed_volume > 0
        # the remainder is still below the stop -> sold on the next tick (second SELL order)
        assert len(sells) >= 2 and sells[-1].state is OrderState.FILLED
        assert not w.svc.portfolio.state.has_position
        assert (await w.svc.reconciler.run([])).ok and w.svc.portfolio.state.btc == w.gw.ledger.btc
        return f"partial sell {sells[0].executed_volume} (cancel) then remainder {sells[-1].executed_volume}; books == ledger"
    finally:
        await w.close()


@scenario("22_cooldown_after_loss_blocks_reentry")
async def s22(tmp: Path) -> str:
    w = await make_world(tmp, cooldown=180)
    await bring_online(w, warm_path())
    await _entered(w)
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)
    await w.feed(p.candles)
    assert not w.svc.portfolio.state.has_position
    before = await _buy_signals(w)
    for _ in range(6):
        await breakout(w, pct=0.03)
        if await _buy_signals(w) > before:
            break
    rej = [e for e in await w.repo.recent_events(300) if e.type == "RISK_REJECTED"]
    try:
        assert any("COOLDOWN" in str(e.payload) for e in rej), [str(e.payload) for e in rej][-3:]
        assert len([x for x in _orders(w) if x.side.value == "bid"]) == 1
        return "re-entry within cooldown rejected"
    finally:
        await w.close()


@scenario("23_orders_per_hour_limit")
async def s23(tmp: Path) -> str:
    w = await make_world(tmp, orders_per_hour=1)
    await bring_online(w, warm_path())
    await _entered(w)
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)
    await w.feed(p.candles)  # stop-out = 2nd order this hour? (limit counts non-rejected orders)
    try:
        # SELL is never blocked by the hourly limit; a new BUY is
        assert not w.svc.portfolio.state.has_position
        before = await _buy_signals(w)
        for _ in range(6):
            await breakout(w, pct=0.03)
            if await _buy_signals(w) > before:
                break
        rej = [e for e in await w.repo.recent_events(300) if e.type == "RISK_REJECTED"]
        assert any("ORDERS_PER_HOUR" in str(e.payload) for e in rej), [str(e.payload) for e in rej][
            -3:
        ]
        return "SELL allowed, next BUY blocked by ORDERS_PER_HOUR"
    finally:
        await w.close()


def _seed_1h(days: int = 30) -> list[Candle]:
    out = []
    t = T0 - timedelta(days=days)
    px = P0 * 0.9
    for i in range(days * 24):
        o = px
        c = o * (1 + (0.0004 if (i // 6) % 2 == 0 else -0.0003))
        out.append(
            Candle(
                "KRW-BTC",
                Timeframe.H1,
                t,
                Decimal(round(o)),
                Decimal(round(max(o, c) * 1.001)),
                Decimal(round(min(o, c) * 0.999)),
                Decimal(round(c)),
                Decimal(10),
                Decimal(round(c * 10)),
            )
        )
        px = c
        t += timedelta(hours=1)
    return out


@scenario("24_real_4h_1d_buckets_breakout_buy")
async def s24(tmp: Path) -> str:
    seed = _seed_1h(30)
    w = await make_world(
        tmp,
        tf="4h",
        trend="1d",
        params={**FAST_PARAMS, "entry_n": 3, "exit_n": 2, "trend_ema": 3, "atr_period": 3},
        seed_1h=seed,
    )
    try:
        assert w.svc.indicators[Timeframe.D1].ready, (
            "1d indicators should be warmed from 1h history"
        )
        assert w.svc.indicators[Timeframe.H4].ready
        start_px = float(seed[-1].close)
        p = Path1m(price=start_px, t=T0).move(
            60 * 12, 0.06
        )  # 12h steady rise = three 4h closes above channel
        await w.feed(p.candles)
        await w.supervise()
        assert _st(w) is BotState.RUNNING
        if not w.svc.portfolio.state.has_position:
            p2 = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(60 * 8, 0.05)
            await w.feed(p2.candles)
        assert w.svc.portfolio.state.has_position
        c = w.svc.strategy_epoch["counters"]
        assert w.svc.strategy_epoch["timeframe"] == "4h", w.svc.strategy_epoch
        assert c["bars_processed"] >= 2, dict(c)
        return f"4h bars processed {c['bars_processed']}, entry after 4h close above channel"
    finally:
        await w.close()


@scenario("25_real_4h_1d_restart_rewarms_from_1h_history")
async def s25(tmp: Path) -> str:
    seed = _seed_1h(30)
    w = await make_world(
        tmp,
        tf="4h",
        trend="1d",
        params={**FAST_PARAMS, "entry_n": 3, "exit_n": 2, "trend_ema": 3, "atr_period": 3},
        seed_1h=seed,
    )
    p = Path1m(price=float(seed[-1].close), t=T0).move(60 * 12, 0.06)
    await w.feed(p.candles)
    w2 = await reopen_world(w)
    try:
        assert w2.svc.indicators[Timeframe.D1].ready and w2.svc.indicators[Timeframe.H4].ready
        assert w2.svc.strategy_epoch["parameter_hash"] == w.svc.strategy_epoch["parameter_hash"]
        assert (
            w2.svc.strategy_epoch["started_at"] == w.svc.strategy_epoch["started_at"]
        )  # same epoch, not reset
        return (
            "restart re-warmed 4h/1d indicators from stored 1h+1m history; strategy epoch preserved"
        )
    finally:
        await w2.close()


@scenario("26_loss_limit_halt_still_allows_protective_stop")
async def s26(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    w.svc.portfolio.daily.start_equity = Decimal(104000)  # -4% today -> next evaluation kills
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)  # breaches the stop
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert _st(w) is BotState.HALTED and w.svc.sm.reason in ("DAILY_LOSS_LIMIT", "MAX_DRAWDOWN")
        assert (
            len(tr) == 1 and not w.svc.portfolio.state.has_position
        )  # protective stop executed under EXIT_ONLY
        assert len([x for x in _orders(w) if x.side.value == "bid"]) == 1  # no new BUY after halt
        return f"HALTED({w.svc.sm.reason}) = EXIT_ONLY: stop sold {tr[0].quantity} BTC, no re-entry"
    finally:
        await w.close()


@scenario("27_paused_still_executes_protective_stop")
async def s27(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    await w.repo.add_command("PAUSE", "op", confirmed=True, command_id="pause-27", ttl_sec=600)
    await w.svc.process_commands(now=datetime.now(UTC))
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(20, -0.06)
    await w.feed(p.candles)
    tr = await w.repo.closed_trades(TradingMode.PAPER)
    try:
        assert _st(w) is BotState.PAUSED and len(tr) == 1 and not w.svc.portfolio.state.has_position
        sells = [x for x in _orders(w) if x.side.value == "ask"]
        assert len(sells) == 1 and sells[0].executed_volume <= Decimal(
            str(tr[0].quantity)
        )  # never more than held
        return "PAUSED = EXIT_ONLY: stop executed, quantity == held"
    finally:
        await w.close()


@scenario("28_flat_after_stop_suppresses_channel_sell")
async def s28(tmp: Path) -> str:
    w = await make_world(tmp)
    await bring_online(w, warm_path())
    await _entered(w)
    p = Path1m(price=float(w.svc.portfolio.price), t=w.now).move(
        15 * 8, -0.12
    )  # stop first, then channel exit at 15m close
    await w.feed(p.candles)
    rej = [e for e in await w.repo.recent_events(400) if e.type == "RISK_REJECTED"]
    sigs = await w.repo.recent_signals(400)
    try:
        assert not w.svc.portfolio.state.has_position
        assert not any(
            "POSITION_STATE_NO_POSITION" in str(e.payload) for e in rej
        )  # suppressed, not rejected
        sells = [x for x in sigs if x.action == "SELL"]
        assert len(sells) >= 1
        return f"{len(sells)} SELL signal(s) recorded; no NO_POSITION rejection events"
    finally:
        await w.close()


async def run_all(only: set[str] | None = None) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for name, fn in SCENARIOS.items():
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="replay_"))
        try:
            detail = await fn(tmp)
            results.append(ScenarioResult(name, True, detail))
        except Exception as exc:
            results.append(ScenarioResult(name, False, f"{type(exc).__name__}: {exc}"[:300]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results
