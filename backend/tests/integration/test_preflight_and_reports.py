from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.monitoring.daily_report import build_daily_report, save_daily_report
from app.monitoring.readiness import evaluate_readiness
from app.services.live_preflight import run_preflight

CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.yaml"


async def _env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, keys: bool
) -> tuple[Settings, Repository]:
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    if keys:
        monkeypatch.setenv("UPBIT_ACCESS_KEY", "AK")
        monkeypatch.setenv("UPBIT_SECRET_KEY", "SK")
    cfg = load_yaml_config(CONFIG)
    # pin approvals: the production yaml is LIVE-approved since 2026-09-30; these tests model the
    # pre-approval state (strategy blocker first)
    cfg.live_gate.enabled = False
    for st in cfg.live_gate.strategy_status.values():
        object.__setattr__(st, "paper_approved", False)
        object.__setattr__(st, "live_approved", False)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/p.db"
    cfg.system.reports_dir = tmp_path / "reports"
    s = Settings(EnvSettings(_env_file=None), cfg)
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    return s, Repository(engine)


def _ip_history(ip: str, n: int = 26) -> list[dict[str, object]]:
    t0 = datetime.now(UTC) - timedelta(hours=n - 1)
    return [{"at": (t0 + timedelta(hours=i)).isoformat(), "ip": ip, "detail": {}} for i in range(n)]


async def test_preflight_blocked_without_keys_and_without_static_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s, repo = await _env(monkeypatch, tmp_path, keys=False)
    r = await run_preflight(s, repo)
    assert not r.ok and r.blocked_reason == "CREDENTIALS_MISSING"
    assert (await repo.get_state("order_test"))["blocked_reason"] == "CREDENTIALS_MISSING"  # type: ignore[index]
    s, repo = await _env(monkeypatch, tmp_path / "b", keys=True)
    r = await run_preflight(s, repo)
    assert not r.ok and r.blocked_reason == "STATIC_PUBLIC_IP_NOT_VERIFIED"
    rd = await evaluate_readiness(s, repo)
    items = {i["name"]: i for i in rd.to_dict()["items"]}
    assert items["Static public IP"]["status"] == "FAIL"
    assert items["orders/test"]["status"] == "NOT_RUN"  # blocked before the private steps
    assert rd.block_reason == "STRATEGY_NOT_APPROVED"  # strategy stays the first-order blocker


@respx.mock
async def test_preflight_runs_accounts_chance_and_orders_test_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s, repo = await _env(monkeypatch, tmp_path, keys=True)
    s.config.live_gate.static_public_ip = "1.2.3.4"
    await repo.set_state("public_ip", {"history": _ip_history("1.2.3.4")})
    respx.get("https://api.upbit.com/v1/accounts").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "currency": "KRW",
                    "balance": "100000",
                    "locked": "0",
                    "avg_buy_price": "0",
                    "avg_buy_price_modified": False,
                    "unit_currency": "KRW",
                }
            ],
        )
    )
    respx.get("https://api.upbit.com/v1/orders/chance").mock(
        return_value=httpx.Response(
            200,
            json={
                "bid_fee": "0.0005",
                "ask_fee": "0.0005",
                "market": {
                    "id": "KRW-BTC",
                    "name": "BTC/KRW",
                    "state": "active",
                    "bid": {"currency": "KRW", "min_total": "5000"},
                    "ask": {"currency": "BTC", "min_total": "5000"},
                    "max_total": "1000000000",
                },
                "bid_account": {
                    "currency": "KRW",
                    "balance": "100000",
                    "locked": "0",
                    "avg_buy_price": "0",
                    "avg_buy_price_modified": False,
                    "unit_currency": "KRW",
                },
                "ask_account": {
                    "currency": "BTC",
                    "balance": "0",
                    "locked": "0",
                    "avg_buy_price": "0",
                    "avg_buy_price_modified": False,
                    "unit_currency": "KRW",
                },
            },
        )
    )
    test_route = respx.post("https://api.upbit.com/v1/orders/test").mock(
        return_value=httpx.Response(
            201,
            json={
                "uuid": "t",
                "side": "bid",
                "ord_type": "price",
                "price": "6000",
                "state": "wait",
                "market": "KRW-BTC",
                "created_at": "2026-09-25T14:00:00+09:00",
            },
        )
    )
    real_route = respx.post("https://api.upbit.com/v1/orders").mock(
        return_value=httpx.Response(201, json={})
    )
    r = await run_preflight(s, repo)
    assert r.ok and [st["name"] for st in r.steps] == [
        "credentials",
        "static_public_ip",
        "accounts",
        "orders_chance",
        "orders_test",
        "challenge_capital",
    ]
    assert test_route.called and not real_route.called  # never a real order
    terms = await repo.get_state("exchange_terms")
    assert terms and terms["min_total"] == "5000"
    rd = await evaluate_readiness(s, repo)
    items = {i["name"]: i for i in rd.to_dict()["items"]}
    assert (
        items["orders/test"]["status"] == "PASS" and items["Static public IP"]["status"] == "PASS"
    )
    assert items["Challenge capital"]["status"] == "PASS"
    assert not rd.live_allowed and rd.block_reason == "STRATEGY_NOT_APPROVED"
    # secrets never stored
    import json

    assert "SK" not in json.dumps(await repo.get_state("order_test"))


@respx.mock
async def test_preflight_auth_failure_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s, repo = await _env(monkeypatch, tmp_path, keys=True)
    s.config.live_gate.static_public_ip = "1.2.3.4"
    await repo.set_state("public_ip", {"history": _ip_history("1.2.3.4")})
    respx.get("https://api.upbit.com/v1/accounts").mock(
        return_value=httpx.Response(
            401, json={"error": {"name": "no_authorization_ip", "message": "ip"}}
        )
    )
    r = await run_preflight(s, repo)
    assert (
        not r.ok
        and r.blocked_reason == "AUTH_FAILED"
        and "no_authorization_ip" in r.steps[-1]["detail"]
    )


async def test_daily_report_build_and_save(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s, repo = await _env(monkeypatch, tmp_path, keys=False)
    day = date(2026, 9, 25)
    await repo.set_state(
        "bot", {"state": "RUNNING", "reason": "RECOVERY_CHECKS_PASSED", "mode": "PAPER"}
    )
    await repo.set_state(
        "portfolio",
        {"state": {"btc": "0", "hwm": "100300", "max_drawdown": "0.004"}, "stop_price": None},
    )
    # snapshots: one before the day, two inside
    await repo.add_snapshot(
        TradingMode.PAPER,
        {
            "krw": Decimal(100000),
            "btc_qty": Decimal(0),
            "btc_price": Decimal(1),
            "btc_value": Decimal(0),
            "equity": Decimal(100000),
            "avg_entry": None,
            "realized_pnl": Decimal(0),
            "unrealized_pnl": Decimal(0),
            "fees_total": Decimal(0),
            "hwm": Decimal(100000),
            "drawdown": Decimal(0),
        },
    )
    from sqlalchemy import update

    from app.database.engine import session_scope
    from app.database.models import PortfolioSnapshotRow

    async with session_scope(repo._factory) as sess:
        await sess.execute(
            update(PortfolioSnapshotRow).values(ts=datetime(2026, 9, 24, 10, tzinfo=UTC))
        )
    await repo.add_snapshot(
        TradingMode.PAPER,
        {
            "krw": Decimal(100300),
            "btc_qty": Decimal(0),
            "btc_price": Decimal(1),
            "btc_value": Decimal(0),
            "equity": Decimal(100300),
            "avg_entry": None,
            "realized_pnl": Decimal(300),
            "unrealized_pnl": Decimal(0),
            "fees_total": Decimal(30),
            "hwm": Decimal(100300),
            "drawdown": Decimal(0),
        },
    )
    tid = await repo.open_trade(
        mode=TradingMode.PAPER,
        entry_order_id=None,
        entry_time=datetime.now(UTC) - timedelta(hours=2),
        entry_price=Decimal(100),
        quantity=Decimal(1),
        fees=Decimal(15),
        strategy="x",
        entry_reason="e",
    )
    await repo.close_trade(
        tid,
        exit_order_id=None,
        exit_time=datetime.now(UTC),
        exit_price=Decimal(110),
        gross_pnl=Decimal(330),
        fees_total=Decimal(30),
        net_pnl=Decimal(300),
        return_pct=Decimal(3),
        exit_reason="x",
    )
    rep = await build_daily_report(
        repo,
        mode=TradingMode.PAPER,
        day=datetime.now(UTC).astimezone().date() if False else day,
        tz="Asia/Seoul",
        initial_equity=Decimal(100000),
        challenge_start=date(2026, 9, 1),
    )
    assert rep.day_number == 25 and rep.start_equity == Decimal(100000)
    md_path = await save_daily_report(repo, rep, s.config.system.reports_dir)
    text = md_path.read_text(encoding="utf-8")
    assert (
        md_path.name == "2026-09-25.md"
        and "DAY 25" in text
        and "| Trading mode | PAPER |" in text
        and "Reconciliation" in text
    )
    rows = await repo.recent_events(1)  # no events required; just ensure repo still works
    assert isinstance(rows, list)
    from sqlalchemy import select

    from app.database.models import DailyStatsRow

    async with repo._factory() as sess:
        row = (await sess.execute(select(DailyStatsRow))).scalar_one()
    assert row.date == "2026-09-25" and row.mode == "PAPER"
    # idempotent re-save updates instead of duplicating
    await save_daily_report(repo, rep, s.config.system.reports_dir)
    async with repo._factory() as sess:
        assert len(list((await sess.execute(select(DailyStatsRow))).scalars())) == 1


def test_launchd_plists_are_valid_and_independent() -> None:
    import plistlib

    root = Path(__file__).resolve().parents[3]
    labels = []
    for name in ("com.quant.trader", "com.quant.api"):
        raw = (
            (root / "scripts" / "launchd" / f"{name}.plist")
            .read_text()
            .replace("__BACKEND__", "/x/backend")
            .replace("__ROOT__", "/x")
        )
        d = plistlib.loads(raw.encode())
        labels.append(d["Label"])
        assert d["KeepAlive"] is True and d["RunAtLoad"] is True
        assert "/x/backend/.venv/bin/python" in d["ProgramArguments"]
    assert labels == ["com.quant.trader", "com.quant.api"]
    trader = plistlib.loads(
        (root / "scripts/launchd/com.quant.trader.plist")
        .read_text()
        .replace("__BACKEND__", "/x")
        .replace("__ROOT__", "/x")
        .replace("__TRADING_MODE__", "PAPER")  # rendered by launchd_install.sh ($TRADING_MODE)
        .encode()
    )
    assert (
        trader["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
        and trader["EnvironmentVariables"]["TRADING_MODE"] == "PAPER"
    )
