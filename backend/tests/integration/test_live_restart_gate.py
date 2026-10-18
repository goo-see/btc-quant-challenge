"""Recent-backtest gate split: PROMOTION (first activation / new epoch / hash change -> fresh
backtest REQUIRED) vs ACTIVE LIVE EPOCH RESTART (stale backtest -> WARNING only, never a block).
Includes the position-holding restart scenario with a stale backtest. No network, no orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.live_guard import LiveEvidence, evidence_gates
from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.state import BotState
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.monitoring.readiness import _frozen_hash, evaluate_readiness
from app.services.trader_service import TraderService
from tests.integration.test_live_preparation import (
    T0,
    FakeLiveGateway,
    _buy_signal,
    _live_service,
)
from tests.integration.test_trader_service import FakeFeed, Probe, _tick

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "config.yaml"


def _settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, mode: str = "LIVE", approved: bool = True
) -> Settings:
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND_THIS_USES_REAL_KRW")
    monkeypatch.setenv("UPBIT_ACCESS_KEY", "x" * 20)
    monkeypatch.setenv("UPBIT_SECRET_KEY", "y" * 40)
    cfg = load_yaml_config(CONFIG)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/gate.db"
    s = Settings(EnvSettings(_env_file=None), cfg)
    st = cfg.live_gate.strategy_status[cfg.strategy.strategy_version]
    # pin the approval flags either way — the production yaml is LIVE-approved since 2026-09-30
    object.__setattr__(st, "paper_approved", approved)
    object.__setattr__(st, "live_approved", approved)
    object.__setattr__(cfg.live_gate, "enabled", approved)
    # the shipped (public) config leaves the whitelist IP empty; the gate scenario needs one
    object.__setattr__(cfg.live_gate, "static_public_ip", "1.2.3.4")
    return s


async def _seed_evidence(repo: Repository, *, backtest_age_days: float) -> None:
    """Everything except the backtest age passes: 48h+ PAPER runtime, IP 25h stable, reconcile OK,
    order_test ok, no critical events."""
    now = datetime.now(UTC)
    await repo.set_state(
        "runtime_PAPER",
        {"running_sec": 60 * 3600, "epoch_start": (now - timedelta(days=3)).isoformat()},
    )
    await repo.set_state(
        "strategy_epoch_PAPER",
        {
            "strategy_version": "trend_following_4h_v1",
            "started_at": (now - timedelta(days=2)).isoformat(),
            "parameter_hash": "c0e59de4f7b70c0c",
            "counters": {},
        },
    )
    await repo.set_state(
        "public_ip",
        {
            "history": [
                {
                    "at": (now - timedelta(hours=30)).isoformat(),
                    "ip": "1.2.3.4",
                    "detail": {},
                },
                {"at": now.isoformat(), "ip": "1.2.3.4", "detail": {}},
            ],
            "latest": "1.2.3.4",
        },
    )
    await repo.add_reconciliation(
        {
            "internal_krw": Decimal(100000),
            "exchange_krw": Decimal(100000),
            "internal_btc": Decimal(0),
            "exchange_btc": Decimal(0),
            "krw_diff": Decimal(0),
            "btc_diff": Decimal(0),
            "status": "OK",
        }
    )
    await repo.set_state(
        "order_test",
        {
            "ok": True,
            "api_ok": True,
            "steps": [
                {"name": "orders_test", "ok": True, "detail": "dry-run"},
                {"name": "challenge_capital", "ok": True, "detail": ""},
            ],
        },
    )
    await repo.set_state(
        "replay_e2e", {"passed": 28, "total": 28, "strategy_version": "trend_following_4h_v1"}
    )
    await repo.add_backtest_run(
        strategy="trend_following_4h",
        params={},
        timeframe="4h",
        start=now - timedelta(days=3000),
        end=now,
        metrics={"profit_factor": 3.7, "net_return_pct": "129.5", "max_drawdown_pct": "11.3"},
        equity_curve_path=None,
    )
    if backtest_age_days > 0:  # backdate the row
        from sqlalchemy import text

        async with repo._factory() as s:
            await s.execute(
                text("update backtest_runs set ts = :t"),
                {"t": (now - timedelta(days=backtest_age_days)).replace(tzinfo=None)},
            )
            await s.commit()


async def _activated_epoch(
    repo: Repository,
    settings: Settings,
    *,
    hash_override: str | None = None,
    activated: bool = True,
) -> None:
    await repo.set_state(
        "strategy_epoch_LIVE",
        {
            "strategy_version": settings.config.strategy.strategy_version,
            "parameter_hash": hash_override or _frozen_hash(settings),
            "started_at": datetime.now(UTC).isoformat(),
            "counters": {},
        },
    )
    ch = {"status": "LIVE_ACTIVATED_WAITING_FIRST_BUY", "initial_capital": "100000"}
    if activated:
        ch["live_activated_at"] = datetime.now(UTC).isoformat()
    await repo.set_state("challenge", ch)


async def _repo(tmp_path: Path) -> Repository:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/gate.db")
    await init_schema(engine)
    return Repository(engine)


def _item(r, name: str):  # type: ignore[no-untyped-def]
    return next(i for i in r.items if i.name == name)


# ----------------------------------------------------------------- pure gate matrix
def test_evidence_gate_matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path)

    def gate(days: float, restart: bool) -> tuple[bool, str]:
        ev = LiveEvidence(
            paper_hours=60,
            paper_trades=0,
            paper_critical_errors=0,
            last_reconcile_ok=True,
            order_test_ok=True,
            static_ip_ok=True,
            days_since_last_backtest=days,
            live_epoch_restart=restart,
        )
        c = next(g for g in evidence_gates(s, ev) if g.name == "recent_backtest")
        return c.passed, c.detail

    assert gate(3, False)[0] is True
    assert gate(20, False) == (
        False,
        "days_since_last_backtest=20.0d > 14d (promotion gate: fresh backtest required)",
    )
    ok, detail = gate(20, True)
    assert ok is True and detail.startswith("WARNING: frozen-param backtest stale (20.0d > 14d)")
    assert gate(3, True)[0] is True and "WARNING" not in gate(3, True)[1]


# ----------------------------------------------------------------- readiness scenarios
async def test_initial_activation_with_stale_backtest_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is False and r.block_reason == "RECENT_BACKTEST"
    assert _item(r, "Frozen-param backtest age").status == "WARN"
    assert _item(r, "LIVE epoch restart context").value == "PROMOTION"


async def test_initial_activation_with_recent_backtest_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=2)
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is True and r.block_reason is None
    assert _item(r, "Frozen-param backtest age").status == "PASS"
    assert _item(r, "Backtest evidence").status == "PASS"


async def test_same_epoch_restart_with_stale_backtest_passes_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    await _activated_epoch(repo, s)
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is True and r.block_reason is None
    assert _item(r, "LIVE epoch restart context").value == "ACTIVE_EPOCH_RESTART"
    age = _item(r, "Frozen-param backtest age")
    assert age.status == "WARN" and "STALE — restart of the active LIVE epoch allowed" in age.detail
    assert _item(r, "Backtest evidence").status == "WARN"
    assert next(g.status for g in r.groups if g.name == "Research") == "WARN"


async def test_strategy_hash_change_with_stale_backtest_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    await _activated_epoch(repo, s, hash_override="deadbeefdeadbeef")
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is False and r.block_reason == "RECENT_BACKTEST"
    assert "same_frozen_hash" in _item(r, "LIVE epoch restart context").detail


async def test_new_live_epoch_not_yet_activated_with_stale_backtest_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    await _activated_epoch(
        repo, s, activated=False
    )  # epoch row exists but LIVE was never activated
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is False and r.block_reason == "RECENT_BACKTEST"
    assert "live_activated" in _item(r, "LIVE epoch restart context").detail


async def test_same_epoch_restart_without_approval_flags_is_not_a_restart_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path, approved=False)  # approvals withdrawn -> promotion rules
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    await _activated_epoch(repo, s)
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is False and r.block_reason == "STRATEGY_NOT_APPROVED"
    assert _item(r, "LIVE epoch restart context").value == "PROMOTION"


async def test_paper_mode_readiness_unaffected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path, mode="PAPER", approved=False)
    repo = await _repo(tmp_path)
    await _seed_evidence(repo, backtest_age_days=20)
    r = await evaluate_readiness(s, repo)
    assert r.live_allowed is False and r.block_reason == "STRATEGY_NOT_APPROVED"
    assert _item(r, "LIVE epoch restart context").value == "PROMOTION"
    assert _item(r, "Frozen-param backtest age").status == "WARN"  # visible, informational in PAPER


# ----------------------------------------------------------------- restart with position + stale backtest
async def test_live_restart_with_position_and_stale_backtest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gw = FakeLiveGateway()
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    await svc.start(run_loops=False)
    await svc.on_ws_message(_tick(Decimal(115_000_000), T0))
    await svc.supervisor_tick(T0 + timedelta(seconds=1))
    gw.clock = T0
    await svc.handle_signal(_buy_signal(115_000_000, 112_000_000, 1_000_000))
    assert gw.place_calls == 1 and svc.portfolio.state.has_position
    # trailing stop ratchets on a later HOLD signal while long, then the process dies
    from app.core.types import Action, Signal, Timeframe

    await svc.handle_signal(
        Signal(
            Action.HOLD,
            0.0,
            "IN_POSITION_TRAIL",
            T0 + timedelta(hours=1),
            "trend_momentum",
            Timeframe.M15,
            {"close": 116_000_000.0},
            Decimal(113_000_000),
        )
    )
    assert svc.portfolio.stop_price == Decimal(113_000_000)
    await svc.stop()
    # readiness for the restart: evidence seeded with a 20-day-old backtest, approvals present
    s = svc.settings
    st = s.config.live_gate.strategy_status.setdefault(
        s.config.strategy.strategy_version,
        s.config.live_gate.status_of(s.config.strategy.strategy_version),
    )
    object.__setattr__(st, "research_approved", True)
    object.__setattr__(st, "paper_approved", True)
    object.__setattr__(st, "live_approved", True)
    object.__setattr__(s.config.live_gate, "enabled", True)
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND_THIS_USES_REAL_KRW")
    monkeypatch.setenv("UPBIT_ACCESS_KEY", "x" * 20)
    monkeypatch.setenv("UPBIT_SECRET_KEY", "y" * 40)
    s2 = Settings(EnvSettings(_env_file=None), s.config)
    await _seed_evidence(repo, backtest_age_days=20)
    r = await evaluate_readiness(s2, repo)
    assert r.live_allowed is True, r.block_reason  # restart NOT blocked (no KeepAlive loop)
    assert _item(r, "Frozen-param backtest age").status == "WARN"
    assert _item(r, "LIVE epoch restart context").value == "ACTIVE_EPOCH_RESTART"
    # the restarted trader restores the position, initial stop and ratcheted trailing stop
    t2 = TraderService(s2, repo, gw, feed_factory=FakeFeed, probe=Probe(), run_id="r2")
    await t2.start(run_loops=False)
    assert t2.portfolio.state.has_position and t2.portfolio.state.btc == svc.portfolio.state.btc
    assert t2.portfolio.stop_price == Decimal(113_000_000)
    rows = await repo.recent_execution_metrics(TradingMode.LIVE)
    assert rows and rows[0].initial_stop == Decimal(112_000_000)
    await t2.on_ws_message(_tick(Decimal(116_000_000), T0 + timedelta(hours=2)))
    await t2.supervisor_tick(T0 + timedelta(hours=2, seconds=1))
    assert (
        t2.sm.state.value == BotState.RUNNING.value
        and t2.reconciler.last is not None
        and t2.reconciler.last.ok
    )
    assert gw.place_calls == 1  # no order on restart
    # protective SELL still works after the restart (stop hit via ticker)
    gw.clock = T0 + timedelta(hours=3)
    await t2.on_ws_message(_tick(Decimal(112_900_000), T0 + timedelta(hours=3)))
    assert gw.place_calls == 2 and t2.portfolio.state.btc == 0
    await t2.stop()
