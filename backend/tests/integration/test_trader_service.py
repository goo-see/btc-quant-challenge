"""End-to-end PAPER pipeline with a fake feed: signals -> risk -> orders -> portfolio,
state transitions (MAINTENANCE/RUNNING/PAUSED/HALTED), outage handling, commands, restart."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.state import BotState
from app.core.types import Action, Candle, OrderState, Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.ws_client import WsHealth
from app.execution.fill_model import FillParams
from app.execution.paper_gateway import PaperGateway
from app.monitoring.health import ProbeResult
from app.services.trader_service import TraderService
from tests.conftest import trend_1m
from tests.unit.test_strategy import PARAMS

CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.yaml"


def _has_pos(svc: TraderService) -> bool:
    return svc.portfolio.state.has_position


def _st(svc: TraderService) -> BotState:
    """Indirection so mypy does not narrow the state literal across awaits."""
    return svc.sm.state


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


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "LIVE_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_yaml_config(CONFIG)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/svc.db"
    cfg.system.warmup_days = 0
    cfg.system.reconcile_interval_sec = 0
    cfg.strategy.version_file = None
    cfg.strategy.name = "trend_momentum"
    cfg.strategy.version = ""
    cfg.strategy.timeframe = "15m"
    cfg.strategy.trend_timeframe = "1h"
    cfg.strategy.params = dict(PARAMS)
    cfg.risk.cooldown_after_loss_min = 0
    cfg.risk.max_orders_per_hour = 1000
    cfg.risk.max_consecutive_losses = 1000
    return Settings(EnvSettings(_env_file=None), cfg)


async def _service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, run_id: str = "t-run"
) -> tuple[TraderService, Repository, PaperGateway, Probe]:
    s = _settings(monkeypatch, tmp_path)
    engine = create_engine(s.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    gw = PaperGateway(
        market="KRW-BTC",
        initial_krw=Decimal(100000),
        fill_params=FillParams(Decimal("0.0005"), Decimal(3)),
        min_order_krw=Decimal(5000),
    )
    probe = Probe()
    svc = TraderService(s, repo, gw, feed_factory=FakeFeed, probe=probe, run_id=run_id)
    return svc, repo, gw, probe


def _tick(price: Decimal, ts: datetime) -> dict[str, Any]:
    return {
        "type": "ticker",
        "code": "KRW-BTC",
        "trade_price": float(price),
        "trade_timestamp": int(ts.timestamp() * 1000),
        "timestamp": int(ts.timestamp() * 1000),
    }


def _candle_msg(c: Candle) -> dict[str, Any]:
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


async def _pump(svc: TraderService, candles: list[Candle]) -> None:
    """Replay 1m candles through the WS handler; ticker before each candle so prices/stops update."""
    for c in candles:
        await svc.on_ws_message(_tick(c.low, c.open_time))  # stop check on the bar low
        await svc.on_ws_message(_tick(c.close, c.open_time + timedelta(seconds=30)))
        await svc.on_ws_message(_candle_msg(c))


async def test_full_paper_pipeline_trades_and_stays_consistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, gw, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        assert _st(svc) is BotState.MAINTENANCE
        candles = trend_1m(4000, up_bars=400, down_bars=300, start_price=100_000_000)
        await _pump(svc, candles[:5])
        await svc.supervisor_tick(candles[5].open_time)  # recovery checks -> RUNNING
        assert _st(svc) is BotState.RUNNING, svc.recovery.last
        await _pump(svc, candles[5:])
        orders = await repo.recent_orders(TradingMode.PAPER, 500)
        filled = [o for o in orders if o.state is OrderState.FILLED]
        assert filled, "expected at least one paper fill"
        buys = [o for o in filled if o.side.value == "bid"]
        assert all(o.req_krw is not None and o.req_krw <= Decimal(30000) for o in buys)
        sigs = await repo.recent_signals(1000)
        assert {s.action for s in sigs} >= {Action.HOLD.value, Action.BUY.value}
        # internal accounting == paper ledger, always
        res = await svc.reconciler.run(svc.orders.inflight())
        assert res.ok, res.details
        assert svc.portfolio.state.krw == gw.ledger.krw and svc.portfolio.state.btc == gw.ledger.btc
        closed = await repo.closed_trades(TradingMode.PAPER)
        if closed:
            assert all(t.exit_reason for t in closed)
        assert _st(svc) is BotState.RUNNING
        assert svc.orders.duplicates_blocked == 0
    finally:
        await svc.stop()


async def test_stop_loss_via_ticker_goes_through_risk_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, _, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        await svc.on_ws_message(_tick(Decimal(100_000_000), ts))
        await svc.supervisor_tick(ts)
        assert _st(svc) is BotState.RUNNING
        # force a position through the real order path
        from app.core.types import Signal

        await svc.handle_signal(
            Signal(
                Action.BUY,
                1.0,
                "TEST_ENTRY",
                ts,
                "trend_momentum",
                Timeframe.M15,
                stop_price=Decimal(95_000_000),
            )
        )
        assert _has_pos(svc) and svc.portfolio.stop_price == Decimal(95_000_000)
        await svc.on_ws_message(_tick(Decimal(96_000_000), ts + timedelta(minutes=1)))
        assert _has_pos(svc)  # above stop
        await svc.on_ws_message(_tick(Decimal(94_000_000), ts + timedelta(minutes=2)))
        assert not _has_pos(svc)
        trades = await repo.closed_trades(TradingMode.PAPER)
        assert len(trades) == 1 and str(trades[0].exit_reason).startswith("STOP@95000000")
        assert Decimal(str(trades[0].net_pnl)) < 0 and svc.portfolio.consecutive_losses == 1
        decisions = await repo.recent_events(50)
        assert any(e.type == "RISK_APPROVED" for e in decisions)
    finally:
        await svc.stop()


async def test_exchange_outage_goes_maintenance_and_auto_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, _, probe = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        t0 = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t0))
        await svc.supervisor_tick(t0)
        assert _st(svc) is BotState.RUNNING
        # data goes stale; exchange REST is down -> expected outage -> MAINTENANCE, not HALTED
        probe.result = ProbeResult(rest_ok=False, exchange_trade_age_sec=None, error="503")
        await svc.supervisor_tick(t0 + timedelta(seconds=200))
        assert _st(svc) is BotState.MAINTENANCE and svc.sm.reason == "EXCHANGE_OUTAGE"
        # still down: stays in MAINTENANCE (no orders), no kill
        await svc.supervisor_tick(t0 + timedelta(seconds=400))
        assert _st(svc) is BotState.MAINTENANCE
        # exchange back but our data still stale -> not yet
        probe.result = ProbeResult(rest_ok=True, exchange_trade_age_sec=1.0)
        await svc.supervisor_tick(t0 + timedelta(seconds=600))
        assert _st(svc) is BotState.MAINTENANCE and "market_data_fresh" in (
            svc.recovery.last.failed if svc.recovery.last else []
        )
        # fresh data arrives -> all six checks pass -> RUNNING automatically
        t1 = t0 + timedelta(seconds=700)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t1))
        await svc.supervisor_tick(t1)
        assert _st(svc) is BotState.RUNNING and svc.sm.reason == "RECOVERY_CHECKS_PASSED"
        saved = await repo.get_state("bot")
        assert saved is not None and saved["state"] == "RUNNING"
    finally:
        await svc.stop()


async def test_local_fault_and_unexplained_stale_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, _, _, probe = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t0))
        await svc.supervisor_tick(t0)
        assert _st(svc) is BotState.RUNNING
        # exchange is trading (fresh trades) but OUR feed is dead -> grace (2x stale) then HALTED
        probe.result = ProbeResult(rest_ok=True, exchange_trade_age_sec=1.0)
        await svc.supervisor_tick(t0 + timedelta(seconds=150))
        assert _st(svc) is BotState.RUNNING  # within grace, feed is expected to reconnect
        await svc.supervisor_tick(t0 + timedelta(seconds=250))
        assert _st(svc) is BotState.HALTED and svc.sm.reason == "UNEXPLAINED_STALE_LOCAL_FAULT"
        # HALTED is sticky: fresh data does not auto-resume
        t1 = t0 + timedelta(seconds=300)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t1))
        await svc.supervisor_tick(t1)
        assert _st(svc) is BotState.HALTED
    finally:
        await svc.stop()


async def test_reconcile_mismatch_halts_and_manual_resume_reruns_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, gw, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t0))
        await svc.supervisor_tick(t0)
        assert _st(svc) is BotState.RUNNING
        gw.ledger.krw -= Decimal(5000)  # exchange balance drifts from our books
        await svc.supervisor_tick(t0 + timedelta(seconds=10))
        assert _st(svc) is BotState.HALTED and svc.sm.reason == "RECONCILE_FAILED"
        # RESUME while mismatch persists: MAINTENANCE -> recovery fails on reconcile -> HALTED again
        await repo.add_command("RESUME", "test", confirmed=True)
        await svc.process_commands()
        assert _st(svc) is BotState.MAINTENANCE
        await svc.supervisor_tick(t0 + timedelta(seconds=20))
        assert _st(svc) is BotState.HALTED and svc.sm.reason == "RECONCILE_FAILED_DURING_RECOVERY"
        # operator fixes the books (here: exchange restores) and resumes -> RUNNING
        gw.ledger.krw += Decimal(5000)
        await repo.add_command("RESUME", "test", confirmed=True)
        await svc.process_commands()
        await svc.on_ws_message(_tick(Decimal(100_000_000), t0 + timedelta(seconds=30)))
        await svc.supervisor_tick(t0 + timedelta(seconds=30))
        assert _st(svc) is BotState.RUNNING
    finally:
        await svc.stop()


async def test_commands_require_confirmation_pause_resume_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, _, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    try:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        await svc.on_ws_message(_tick(Decimal(100_000_000), t0))
        await svc.supervisor_tick(t0)
        assert _st(svc) is BotState.RUNNING
        row = await repo.add_command("KILL", "test", confirmed=False)
        await svc.process_commands()
        assert _st(svc) is BotState.RUNNING
        assert await repo.pending_commands() == []
        got = await repo.get_command(row.command_id)
        assert (
            got is not None
            and got.status == "REJECTED"
            and "confirmation" in str(got.failure_reason)
        )
        await repo.add_command("PAUSE", "test", confirmed=True)
        await svc.process_commands()
        assert _st(svc) is BotState.PAUSED and not svc.sm.trading_allowed
        await repo.add_command("RESUME", "test", confirmed=True)
        await svc.process_commands()
        assert _st(svc) is BotState.MAINTENANCE
        await svc.supervisor_tick(t0 + timedelta(seconds=5))
        assert _st(svc) is BotState.RUNNING
        await repo.add_command("KILL", "test", confirmed=True)
        await svc.process_commands()
        assert _st(svc) is BotState.HALTED and svc.kill.reason == "MANUAL_KILL"
    finally:
        await svc.stop()


async def test_restart_recovers_portfolio_ledger_and_halted_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, _, gw, _ = await _service(monkeypatch, tmp_path, run_id="run-1")
    await svc.start(run_loops=False)
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    await svc.on_ws_message(_tick(Decimal(100_000_000), ts))
    await svc.supervisor_tick(ts)
    from app.core.types import Signal

    await svc.handle_signal(
        Signal(
            Action.BUY,
            1.0,
            "ENTRY",
            ts,
            "trend_momentum",
            Timeframe.M15,
            stop_price=Decimal(90_000_000),
        )
    )
    assert svc.portfolio.state.has_position
    await svc.kill.trigger("MANUAL_KILL", {})
    snapshot = (
        svc.portfolio.state.krw,
        svc.portfolio.state.btc,
        svc.portfolio.stop_price,
        gw.ledger.krw,
        gw.ledger.btc,
    )
    await svc.stop()  # process "crashes"/exits

    # new process, same DB, fresh gateway object with default ledger
    s2 = _settings(monkeypatch, tmp_path)
    engine = create_engine(s2.config.system.database_url)
    repo2 = Repository(engine)
    gw2 = PaperGateway(
        market="KRW-BTC",
        initial_krw=Decimal(100000),
        fill_params=FillParams(Decimal("0.0005"), Decimal(3)),
    )
    svc2 = TraderService(s2, repo2, gw2, feed_factory=FakeFeed, probe=Probe(), run_id="run-2")
    await svc2.start(run_loops=False)
    try:
        assert (
            _st(svc2) is BotState.HALTED and svc2.sm.reason == "MANUAL_KILL"
        )  # sticky across restart
        assert (
            svc2.portfolio.state.krw,
            svc2.portfolio.state.btc,
            svc2.portfolio.stop_price,
            gw2.ledger.krw,
            gw2.ledger.btc,
        ) == snapshot
        assert svc2.portfolio.open_trade_id is not None
        await svc2.on_ws_message(_tick(Decimal(100_000_000), ts + timedelta(minutes=5)))
        await svc2.supervisor_tick(ts + timedelta(minutes=5))
        assert _st(svc2) is BotState.HALTED  # no auto resume
        await repo2.add_command("RESUME", "operator", confirmed=True)
        await svc2.process_commands()
        await svc2.supervisor_tick(ts + timedelta(minutes=5, seconds=5))
        assert (
            _st(svc2) is BotState.RUNNING
        )  # recovery checks pass: ledger == books, position == trade row
        res = await svc2.reconciler.run([])
        assert res.ok
    finally:
        await svc2.stop()
        await engine.dispose()


async def test_live_mode_cannot_use_paper_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    s = Settings(EnvSettings(_env_file=None), s.config)
    engine = create_engine(s.config.system.database_url)
    gw = PaperGateway(market="KRW-BTC", initial_krw=Decimal(1), fill_params=FillParams())
    with pytest.raises(RuntimeError, match="LIVE mode cannot run on the paper gateway"):
        TraderService(s, Repository(engine), gw)
    await engine.dispose()
