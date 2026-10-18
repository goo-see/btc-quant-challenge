"""ETH LIVE EXPERIMENT restart gate (Phase 2) — READ-ONLY. Exit 0 only if EVERY check passes.

usage: cd backend && uv run python ../scripts/eth_live_gate.py [--pre reports/snapshots/PRE_LIVE_ETH_SNAPSHOT.json]
Checks: BTC position/stop restored vs the PRE snapshot, D0 frozen hash, trader reconciliation,
exchange balances vs books, single trader process, E1 shadow health, DB integrity, sleeves
loaded (carve-out done, ETH ledger key), allocations 50k/50k, next D0 entry 15k, cash isolation.
Never places an order; never writes to any database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import httpx

from app.config.settings import Settings
from app.exchange.upbit.rest_client import UpbitRestClient
from app.monitoring.readiness import _frozen_hash
from app.portfolio.sleeves import sizing_caps
from app.risk.position_sizer import SizingParams, size_buy_krw

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8787"


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", default=str(ROOT / "reports/snapshots/PRE_LIVE_ETH_SNAPSHOT.json"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    pre = json.loads(Path(args.pre).read_text()) if Path(args.pre).exists() else {}
    s = Settings.load()
    cfg = s.config
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    async with httpx.AsyncClient(base_url=API, timeout=10) as c:
        status = (await c.get("/api/status")).json()
        pf = (await c.get("/api/portfolio")).json()
        health = (await c.get("/api/health")).json()
        sleeves = (await c.get("/api/portfolio/sleeves")).json()
        e1 = (await c.get("/api/research/e1")).json()
        decision = (await c.get("/api/decision")).json()

    pre_pf = pre.get("portfolio", {})
    pre_pid = (pre.get("status") or {}).get("pid")
    # 1. trader restarted, LIVE, RUNNING, ENTRY_AND_EXIT
    chk(
        "trader_online",
        bool(status.get("online")),
        f"heartbeat age {status.get('heartbeat_age_sec')}",
    )
    chk("trader_mode_live", status.get("mode") == "LIVE", str(status.get("mode")))
    chk(
        "trader_running",
        status.get("state") == "RUNNING",
        f"{status.get('state')} / {status.get('state_reason')}",
    )
    chk(
        "execution_entry_and_exit",
        status.get("execution_mode") == "ENTRY_AND_EXIT",
        str(status.get("execution_mode")),
    )
    chk(
        "new_pid",
        status.get("pid") != pre_pid and status.get("pid") is not None,
        f"pre {pre_pid} -> now {status.get('pid')}",
    )
    # 2. position / stop restored
    if pre_pf:
        chk(
            "btc_position_restored",
            _d(pf["btc"]) == _d(pre_pf["btc"]),
            f"{pre_pf['btc']} -> {pf['btc']}",
        )
        chk(
            "btc_entry_restored",
            _d(pf["avg_entry"] or 0) == _d(pre_pf["avg_entry"] or 0),
            f"{pre_pf['avg_entry']} -> {pf['avg_entry']}",
        )
        stop_now, stop_pre = _d(pf["stop_price"] or 0), _d(pre_pf["stop_price"] or 0)
        chk("btc_stop_not_lowered", stop_now >= stop_pre, f"{stop_pre} -> {stop_now}")
    else:
        chk("pre_snapshot_present", False, "PRE snapshot missing")
    # 3. frozen hash
    con = sqlite3.connect(f"file:{ROOT}/data/quant.db?mode=ro", uri=True, timeout=10)
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    epoch = json.loads(q("select value from bot_state where key='strategy_epoch_LIVE'")[0][0])
    chk(
        "d0_frozen_hash",
        epoch.get("parameter_hash") == _frozen_hash(s) == "c0e59de4f7b70c0c",
        f"epoch {epoch.get('parameter_hash')} config {_frozen_hash(s)}",
    )
    # 4. reconciliation
    rec = health.get("reconciliation") or {}
    rec_at = datetime.fromisoformat(rec["at"]) if rec.get("at") else None
    fresh = rec_at is not None and (datetime.now(UTC) - rec_at).total_seconds() < 900
    chk(
        "trader_reconcile_ok_fresh",
        rec.get("status") == "OK" and fresh,
        f"{rec.get('status')} at {rec.get('at')} krw_diff {rec.get('krw_diff')}",
    )
    # 5. exchange balances vs books (read-only private API)
    env = s.env
    assert env.upbit_access_key and env.upbit_secret_key
    async with UpbitRestClient(
        env.upbit_access_key.get_secret_value(), env.upbit_secret_key.get_secret_value()
    ) as rc:
        accts = {a.currency: a.balance + a.locked for a in await rc.get_accounts()}
        open_btc = await rc.get_open_orders("KRW-BTC", states=("wait", "watch"))
        open_eth = await rc.get_open_orders("KRW-ETH", states=("wait", "watch"))
    transfers = {}
    rows = q("select value from bot_state where key='sleeve_transfers'")
    if rows:
        transfers = json.loads(rows[0][0])
    carve = transfers.get("ETH_GRID") or {}
    d0_cash = _d(pf["krw"])
    eth_ledger_rows = q("select value from bot_state where key='sleeve_ledger_ETH_GRID'")
    eth_ledger = json.loads(eth_ledger_rows[0][0]) if eth_ledger_rows else {}
    eth_cash = _d(eth_ledger.get("cash", 50000 if carve.get("status") == "DONE" else 0))
    eth_qty = _d(eth_ledger.get("quantity", 0))
    krw_ex = accts.get("KRW", Decimal(0))
    chk(
        "exchange_krw_matches_books",
        abs(krw_ex - (d0_cash + eth_cash)) <= cfg.kill_switch.reconcile_krw_tolerance,
        f"exchange {krw_ex} vs D0 {d0_cash} + ETH {eth_cash}",
    )
    chk(
        "exchange_btc_matches_book",
        abs(accts.get("BTC", Decimal(0)) - _d(pf["btc"]))
        <= cfg.kill_switch.reconcile_btc_tolerance,
        f"exchange {accts.get('BTC')} vs book {pf['btc']}",
    )
    chk(
        "exchange_eth_matches_ledger",
        abs(accts.get("ETH", Decimal(0)) - eth_qty) <= cfg.kill_switch.reconcile_btc_tolerance,
        f"exchange {accts.get('ETH', 0)} vs ledger {eth_qty}",
    )
    chk("no_open_orders", not open_btc and not open_eth, f"BTC {len(open_btc)} ETH {len(open_eth)}")
    # 6. single trader process
    out = subprocess.run(  # noqa: ASYNC221
        ["/usr/bin/pgrep", "-fl", "app.main_trader"], capture_output=True, text=True
    )
    procs = [line for line in out.stdout.splitlines() if "caffeinate" not in line]
    chk("single_trader_process", len(procs) == 1, "; ".join(procs))
    # 7. E1 shadow
    r = e1.get("runner") or {}
    chk(
        "e1_shadow_running",
        e1.get("status") == "SHADOW"
        and r.get("ws_connected")
        and (r.get("errors") or 0) == 0
        and r.get("pid") == 14384,
        f"pid {r.get('pid')} ws {r.get('ws_connected')} errors {r.get('errors')}",
    )
    # 8. DB integrity
    chk(
        "db_integrity", q("pragma integrity_check")[0][0] == "ok", q("pragma integrity_check")[0][0]
    )
    # 9. sleeves loaded (carve-out + own ledger)
    chk(
        "sleeve_carve_out_done",
        carve.get("status") == "DONE" and _d(carve.get("amount", 0)) == 50000,
        json.dumps(
            {k: v for k, v in carve.items() if k not in ("book_before", "book_after")}, default=str
        )[:200],
    )
    d0_ledger_rows = q("select value from bot_state where key='sleeve_ledger_BTC_D0'")
    d0_ledger = json.loads(d0_ledger_rows[0][0]) if d0_ledger_rows else {}
    chk(
        "d0_ledger_written_by_new_trader",
        bool(d0_ledger)
        and d0_ledger.get("owner") == "trader"
        and _d(d0_ledger.get("cash", -1)) == d0_cash,
        json.dumps(d0_ledger)[:200],
    )
    con.close()
    d0 = (sleeves.get("sleeves") or {}).get("BTC_D0") or {}
    eth = (sleeves.get("sleeves") or {}).get("ETH_GRID") or {}
    chk(
        "btc_d0_allocation_50k",
        _d(d0.get("allocated_capital", 0)) == 50000,
        str(d0.get("allocated_capital")),
    )
    chk(
        "eth_grid_allocation_50k",
        _d(eth.get("allocated_capital", 0)) == 50000 and eth.get("mode") == "LIVE_EXPERIMENT",
        f"{eth.get('allocated_capital')} {eth.get('mode')}",
    )
    chk(
        "next_d0_max_entry_15k",
        _d(d0.get("next_max_entry_krw", 0)) == 15000,
        str(d0.get("next_max_entry_krw")),
    )
    chk(
        "grandfathered_position_kept",
        bool(d0.get("grandfathered_position"))
        and _d(d0.get("position_quantity", 0)) == _d(pf["btc"]),
        f"{d0.get('position_quantity')} grandfathered={d0.get('grandfathered_position')}",
    )
    # 10. cash isolation: D0 sizing cannot touch the ETH reserve; ETH cannot touch the D0 reserve
    pos_val = _d(pf["btc_value"])
    _eq, cash = sizing_caps(Decimal(50000), d0_cash + pos_val, d0_cash, pos_val)
    sz = SizingParams(
        Decimal("0.30"), Decimal(30000), Decimal(5000), Decimal("0.0005"), Decimal(1000)
    )
    amt, why = size_buy_krw(Decimal(50000), Decimal(50000), sz)
    chk(
        "d0_cash_excludes_eth_reserve",
        d0_cash + eth_cash <= krw_ex + cfg.kill_switch.reconcile_krw_tolerance
        and cash <= Decimal(50000) - pos_val + Decimal("0.01"),
        f"D0 cash {d0_cash} (cap {cash}) ETH cash {eth_cash}; D0 next entry {amt} ({why})",
    )
    chk("eth_cannot_spend_btc_reserve", eth_cash <= 50000, f"ETH ledger cash {eth_cash} <= 50000")
    chk(
        "decision_snapshot_live",
        decision.get("strategy_version") == "trend_following_4h_v1",
        str(decision.get("strategy_version")),
    )

    ok_all = all(ok for _, ok, _ in checks)
    if args.json:
        print(
            json.dumps(
                {"ok": ok_all, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]},
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        print("ETH LIVE RESTART GATE")
        for n, o, d in checks:
            print(f"  {'PASS' if o else 'FAIL':<5} {n:<36} {d}")
        print("RESULT:", "PASS" if ok_all else "FAIL -> ETH_LIVE_ABORTED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
