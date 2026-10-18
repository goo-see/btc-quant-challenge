"""Read-only summaries for the dashboard: ETH grid shadow state and the strategy-sleeve view."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

SHADOW_DB = Path(__file__).resolve().parents[4] / "data" / "eth_grid_shadow.db"


def grid_summary(path: Path = SHADOW_DB) -> dict[str, Any]:
    if not path.exists():
        return {"status": "NOT_INITIALIZED"}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exp = con.execute(
            "select candidate, params, started_at, seed_price from experiment where id=1"
        ).fetchone()
        st = con.execute("select engine_json, updated_at from state where id=1").fetchone()
        health = con.execute(
            "select pid, started_at, last_tick_at, ticks, ws_connected, errors, updated_at from health where id=1"
        ).fetchone()
        cyc = con.execute(
            "select count(*), coalesce(sum(net),0), coalesce(sum(case when net>0 then 1 else 0 end),0), "
            "coalesce(avg(net),0), coalesce(sum(fees),0) from cycles"
        ).fetchone() or (0, 0, 0, 0, 0)
        today = con.execute(
            "select coalesce(sum(trades),0) from daily where day = date('now','+9 hours')"
        ).fetchone()
        engine = json.loads(st[0]) if st else {}
        s = engine.get("state", {})
        units = engine.get("units", [])
        last_price = Decimal(str(s.get("last_price", "0") or "0"))
        inv_qty = sum((Decimal(u["quantity"]) for u in units), Decimal(0))
        inv_cost = sum((Decimal(u["cost"]) for u in units), Decimal(0))
        inv_value = (inv_qty * last_price).quantize(Decimal("0.01"))
        cash = Decimal(str(s.get("cash", "50000")))
        equity = cash + inv_value
        return {
            "status": "SHADOW",
            "candidate": exp[0] if exp else None,
            "params": json.loads(exp[1]) if exp else None,
            "started_at": exp[2] if exp else None,
            "seed_price": exp[3] if exp else None,
            "runner": {
                "pid": health[0],
                "last_tick_at": health[2],
                "ticks": health[3],
                "ws_connected": bool(health[4]),
                "errors": health[5],
                "updated_at": health[6],
            }
            if health
            else None,
            "virtual_capital": 50000.0,
            "cash": float(cash),
            "inventory_qty": float(inv_qty),
            "inventory_value": float(inv_value),
            "inventory_cost": float(inv_cost),
            "equity": float(equity),
            "realized": float(Decimal(str(s.get("realized", "0")))),
            "unrealized": float(inv_value - inv_cost),
            "fees": float(Decimal(str(s.get("fees", "0")))),
            "completed_cycles": cyc[0],
            "cycle_net_sum": float(cyc[1]),
            "cycle_wins": cyc[2],
            "cycle_avg_net": float(cyc[3]),
            "buys": s.get("buys", 0),
            "sells": s.get("sells", 0),
            "units": len(units),
            "anchor": s.get("anchor"),
            "gap": s.get("gap"),
            "mdd_pct": float(Decimal(str(s.get("mdd", "0"))) * 100),
            "halted": bool(s.get("halted")),
            "halt_reason": s.get("halt_reason"),
            "day_paused": bool(s.get("day_paused")),
            "trend_ok": bool(s.get("trend_ok", True)),
            "ema": s.get("ema"),
            "last_price": float(last_price),
            "trades_today": today[0] if today else 0,
            "next_sell": str(min((Decimal(u["target"]) for u in units), default=Decimal(0))),
            "candidate_status": "UNDER_VALIDATION",
            "note": "연구용 가상 거래 — 실제 ETH 주문 없음",
        }
    finally:
        con.close()
