"""Dashboard API: read-only routes from DB state, control auth/CSRF/confirmation/idempotency/TTL."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.api.deps import ApiState
from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from tests.conftest import synthetic_1m

CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.yaml"
TOKEN = "test-control-token-0123456789"
CTRL = {
    "X-Control-Token": TOKEN,
    "X-Requested-With": "quant-dashboard",
    "Tailscale-User-Login": "alice@example.com",
}


async def _client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[httpx.AsyncClient, Repository, Settings]:
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", TOKEN)
    cfg = load_yaml_config(CONFIG)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    # pin approvals for the test (the production config is LIVE-approved since 2026-09-30)
    cfg.live_gate.enabled = False
    for st in cfg.live_gate.strategy_status.values():
        object.__setattr__(st, "paper_approved", False)
        object.__setattr__(st, "live_approved", False)
    settings = Settings(EnvSettings(_env_file=None), cfg)
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    app = create_app(settings, repo, serve_frontend=False)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    app.state.api = ApiState(settings=settings, repo=repo)  # lifespan not run by ASGITransport
    return client, repo, settings


async def _seed(repo: Repository) -> None:
    now = datetime.now(UTC)
    await repo.set_state(
        "bot",
        {
            "state": "RUNNING",
            "reason": "RECOVERY_CHECKS_PASSED",
            "since": now.isoformat(),
            "run_id": "r1",
            "mode": "PAPER",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "heartbeat": now.isoformat(),
            "strategy": "trend_momentum",
            "pid": 1,
        },
    )
    await repo.set_state(
        "market",
        {"price": "100000000", "price_at": now.isoformat(), "last_data_at": now.isoformat()},
    )
    await repo.set_state(
        "portfolio",
        {
            "state": {
                "initial_equity": "100000",
                "krw": "70000",
                "btc": "0.0003",
                "avg_entry": "100000000",
                "entry_fees": "15",
                "entry_time": now.isoformat(),
                "highest_since_entry": "100000000",
                "realized_pnl": "500",
                "fees_total": "45",
                "hwm": "100500",
                "max_drawdown": "0.012",
                "trade_count": 2,
            },
            "open_trade_id": 1,
            "stop_price": "95000000",
            "entry_reason": "GOLDEN_CROSS",
            "consecutive_losses": 0,
            "last_loss_at": None,
            "daily": {
                "day_key": "2026-09-25",
                "start_equity": "99800",
                "trades": 1,
                "wins": 1,
                "losses": 0,
                "fees": "10",
                "realized": "300",
            },
        },
    )
    await repo.set_state("runtime_PAPER", {"running_sec": 3600 * 29})
    await repo.upsert_candles(
        synthetic_1m(
            120,
            start_price=100_000_000.0,
            start=(now - timedelta(minutes=120)).replace(second=0, microsecond=0),
        )
    )
    await repo.add_reconciliation(
        {
            "internal_krw": Decimal(70000),
            "exchange_krw": Decimal(70000),
            "internal_btc": Decimal("0.0003"),
            "exchange_btc": Decimal("0.0003"),
            "krw_diff": Decimal(0),
            "btc_diff": Decimal(0),
            "status": "OK",
        }
    )
    await repo.add_event("INFO", "BOT_STARTED", "started", {})
    tid = await repo.open_trade(
        mode=TradingMode.PAPER,
        entry_order_id=None,
        entry_time=now - timedelta(hours=1),
        entry_price=Decimal(99_000_000),
        quantity=Decimal("0.0003"),
        fees=Decimal(15),
        strategy="trend_momentum",
        entry_reason="x",
    )
    await repo.close_trade(
        tid,
        exit_order_id=None,
        exit_time=now - timedelta(minutes=30),
        exit_price=Decimal(101_000_000),
        gross_pnl=Decimal(600),
        fees_total=Decimal(30),
        net_pnl=Decimal(570),
        return_pct=Decimal("1.9"),
        exit_reason="EMA_DEATH_CROSS",
    )
    await repo.open_trade(
        mode=TradingMode.PAPER,
        entry_order_id=None,
        entry_time=now - timedelta(minutes=10),
        entry_price=Decimal(100_000_000),
        quantity=Decimal("0.0003"),
        fees=Decimal(15),
        strategy="trend_momentum",
        entry_reason="GOLDEN_CROSS",
    )


async def test_read_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, repo, _ = await _client(monkeypatch, tmp_path)
    await _seed(repo)
    st = (await client.get("/api/status")).json()
    assert (
        st["state"] == "RUNNING"
        and st["online"] is True
        and st["mode"] == "PAPER"
        and st["halted_position_warning"] is None
    )
    pf = (await client.get("/api/portfolio")).json()
    assert (
        pf["equity"] == 100000.0
        and pf["has_position"]
        and pf["unrealized_pnl"] == -15.0
        and pf["stop_price"] == 95_000_000.0
    )
    assert pf["today_pnl"] == 200.0 and pf["goal_progress_pct"] == 0.0
    s = (await client.get("/api/stats")).json()
    assert s["trade_count"] == 1 and s["wins"] == 1 and s["win_rate"] == 1.0
    tr = (await client.get("/api/trades")).json()
    assert len(tr) == 1 and tr[0]["exit_reason"] == "EMA_DEATH_CROSS"
    c = (await client.get("/api/candles?tf=15m&limit=50")).json()
    assert c["candles"] and c["open_position"]["entry_price"] == 100_000_000.0
    assert [m["side"] for m in c["markers"]] == ["BUY", "SELL", "BUY"]
    h = (await client.get("/api/health")).json()
    assert (
        h["trader"] == "ONLINE"
        and h["database"] == "OK"
        and h["reconciliation"]["status"] == "OK"
        and "tailscale" in h
    )
    r = (await client.get("/api/readiness")).json()
    assert r["live_allowed"] is False and r["block_reason"] == "STRATEGY_NOT_APPROVED"
    groups = {g["name"]: g for g in r["groups"]}
    assert set(groups) == {"Research", "Infrastructure", "Private API", "Strategy PAPER", "LIVE"}
    infra = {i["name"]: i for i in groups["Infrastructure"]["items"]}
    strat = {i["name"]: i for i in groups["Strategy PAPER"]["items"]}
    api_g = {i["name"]: i for i in groups["Private API"]["items"]}
    assert infra["Runtime"]["value"].startswith("29.0 / 48h")
    assert (
        strat["Natural trades"]["value"].startswith("1 (")
        and strat["Natural trades"]["status"] == "INFO"
    )
    assert strat["paper_approval_ready"]["value"] == "false" and strat["Strategy runtime"][
        "value"
    ].endswith("/ 24h")
    assert api_g["orders/test"]["status"] == "NOT_RUN" and groups["LIVE"]["status"] == "BLOCKED"
    ev = (await client.get("/api/events")).json()
    assert ev and ev[0]["type"] == "BOT_STARTED"
    assert (await client.get("/api/content/today")).json()["equity"] == 100000.0


async def test_halted_with_open_position_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, repo, _ = await _client(monkeypatch, tmp_path)
    await _seed(repo)
    bot = await repo.get_state("bot") or {}
    await repo.set_state("bot", {**bot, "state": "HALTED", "reason": "MAX_DRAWDOWN"})
    st = (await client.get("/api/status")).json()
    w = st["halted_position_warning"]
    assert (
        w
        and w["halted_reason"] == "MAX_DRAWDOWN"
        and w["quantity_btc"] == 0.0003
        and w["trading_disabled"] is True
    )
    assert (
        w["entry_price"] == 100_000_000.0
        and w["current_price"] == 100_000_000.0
        and w["stop_price"] == 95_000_000.0
    )


async def test_offline_when_heartbeat_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, repo, _ = await _client(monkeypatch, tmp_path)
    await _seed(repo)
    bot = await repo.get_state("bot") or {}
    await repo.set_state(
        "bot", {**bot, "heartbeat": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}
    )
    st = (await client.get("/api/status")).json()
    assert st["online"] is False and st["uptime_sec"] is None
    assert (await client.get("/api/health")).json()["trader"] == "OFFLINE"


async def test_control_auth_csrf_confirmation_idempotency_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, repo, settings = await _client(monkeypatch, tmp_path)
    body: dict[str, Any] = {"command_id": "cmd-00000001", "confirm": "PAUSE"}
    assert (await client.post("/api/control/PAUSE", json=body)).status_code == 401  # no token
    assert (
        await client.post(
            "/api/control/PAUSE",
            json=body,
            headers={
                "X-Control-Token": "wrong-token-000000000",
                "X-Requested-With": "quant-dashboard",
            },
        )
    ).status_code == 401
    assert (
        await client.post("/api/control/PAUSE", json=body, headers={"X-Control-Token": TOKEN})
    ).status_code == 403  # CSRF header missing
    assert (
        await client.post("/api/control/PAUSE", json={**body, "confirm": "KILL"}, headers=CTRL)
    ).status_code == 400
    r = await client.post("/api/control/PAUSE", json=body, headers=CTRL)
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    r2 = await client.post(
        "/api/control/PAUSE", json=body, headers=CTRL
    )  # same command_id -> same row
    assert r2.json()["requested_at"] == r.json()["requested_at"]
    rows = await repo.recent_commands()
    assert (
        len(rows) == 1
        and rows[0].requested_by == "alice@example.com"
        and rows[0].audit["login"] == "alice@example.com"
    )
    exp = datetime.fromisoformat(r.json()["expires_at"])
    assert (
        abs((exp - datetime.now(UTC)).total_seconds() - settings.config.system.command_ttl_sec) < 5
    )
    k = await client.post(
        "/api/control/KILL", json={"command_id": "cmd-kill-0001", "confirm": "KILL"}, headers=CTRL
    )
    kexp = datetime.fromisoformat(k.json()["expires_at"])
    assert (
        abs(
            (kexp - datetime.now(UTC)).total_seconds() - settings.config.system.kill_command_ttl_sec
        )
        < 5
    )
    listed = (await client.get("/api/commands")).json()
    assert {c["type"] for c in listed} == {"PAUSE", "KILL"} and all(
        c["status"] == "PENDING" for c in listed
    )


async def test_control_disabled_without_token_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _, settings = await _client(monkeypatch, tmp_path)
    monkeypatch.delenv("DASHBOARD_CONTROL_TOKEN")
    settings.env.dashboard_control_token = None
    assert os.environ.get("DASHBOARD_CONTROL_TOKEN") is None
    r = await client.post(
        "/api/control/KILL", json={"command_id": "cmd-kill-0002", "confirm": "KILL"}, headers=CTRL
    )
    assert r.status_code == 503
