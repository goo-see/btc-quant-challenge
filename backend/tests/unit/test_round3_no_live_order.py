"""Round 3: SHADOW ONLY — no path from the research package to orders, exchanges or the trader.

Static (AST) guarantees + a runtime guarantee that the market DB handle is read-only.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from app.core.types import Timeframe
from app.research.round3.market_reader import MarketReader

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
PACKAGE = BACKEND / "app" / "research" / "round3"
SCRIPT = ROOT / "scripts" / "research_round3.py"

FORBIDDEN_MODULES = (
    "app.exchange",  # Upbit REST/WS/LiveGateway
    "app.execution.order_manager",
    "app.execution.paper_gateway",
    "app.risk.risk_manager",
    "app.risk.kill_switch",
    "app.services",  # trader service / live preflight
    "app.config.settings",  # would load .env (API keys)
    "app.config.live_guard",
    "app.database",  # SQLAlchemy models/repository of quant.db (write-capable)
    "app.api",
    "httpx",
    "websockets",
    "requests",
    "urllib",
    "socket",
    "aiohttp",
    "subprocess",
)
FORBIDDEN_NAMES = (
    "create_order",
    "test_order",
    "cancel_order",
    "place_order",
    "OrderManager",
    "LiveGateway",
    "PaperGateway",
    "RiskManager",
    "UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY",
)


def _sources() -> list[Path]:
    files = [*sorted(PACKAGE.glob("*.py")), SCRIPT]
    assert len(files) >= 7
    return files


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _transitive_app_imports(start: set[str]) -> set[str]:
    seen: set[str] = set()
    todo = [m for m in start if m.startswith("app.")]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        p = BACKEND / (m.replace(".", "/") + ".py")
        if not p.exists():
            p = BACKEND / m.replace(".", "/") / "__init__.py"
        if p.exists():
            todo.extend(x for x in _imports(p) if x.startswith("app.") and x not in seen)
    return seen


def test_no_forbidden_imports_direct_or_transitive() -> None:
    direct: set[str] = set()
    for f in _sources():
        direct |= _imports(f)
    closure = _transitive_app_imports(direct) | direct
    bad = sorted(m for m in closure if m.startswith(FORBIDDEN_MODULES))
    assert bad == [], f"forbidden modules reachable from Round 3 code: {bad}"


def test_no_order_or_secret_identifiers_in_source() -> None:
    for f in _sources():
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        hit = [x for x in FORBIDDEN_NAMES if x in names]
        assert not hit, f"{f.name}: {hit}"
        assert "0.0.0.0" not in src and "http://" not in src.replace("http://127.0.0.1", "")


def test_market_db_connection_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "market.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table candles (market text, timeframe text, open_time text, open, high, low, close, volume, quote_volume, source text)"
    )
    con.execute("create table bot_state (key text primary key, value text)")
    con.execute(
        "insert into candles values ('KRW-BTC','1m','2026-01-01 00:00:00.000000',1,2,0.5,1.5,1,1,'ws')"
    )
    con.commit()
    con.close()
    reader = MarketReader(db, "KRW-BTC")
    assert len(reader.load(Timeframe.M1)) == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute(
            "insert into candles values ('KRW-BTC','1m','2026-01-01 00:01:00.000000',1,2,0.5,1.5,1,1,'ws')"
        )
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute("delete from candles")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute("update bot_state set value='x'")
    reader.close()


def test_research_db_is_separate_from_market_db() -> None:
    from app.research.round3.config import DEFAULT_CONFIG, load_config

    cfg = load_config(DEFAULT_CONFIG)
    assert cfg.research_db.resolve() != cfg.market_db.resolve()
    assert cfg.research_db.name == "research_round3.db"


def test_launchd_plist_runs_only_the_shadow_runner() -> None:
    plist = (ROOT / "scripts/launchd/com.quant.shadow-round3.plist").read_text()
    assert "app.research.round3.runner" in plist
    assert "TRADING_MODE" not in plist and "LIVE" not in plist
    assert "main_trader" not in plist and "main_api" not in plist
