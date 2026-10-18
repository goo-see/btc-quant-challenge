"""Read-only summary of the E1 forward DB for the dashboard (sync; run in a worker thread)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

FORWARD_DB = Path(__file__).resolve().parents[4] / "data" / "d0_v2_e1_forward.db"

_TRADE_AGG = (
    "select count(*), coalesce(sum(net_pnl),0), "
    "coalesce(sum(case when net_pnl>0 then 1 else 0 end),0), "
    "coalesce(sum(case when mfe_r>=1 and net_pnl<0 then 1 else 0 end),0), "
    "coalesce(sum(case when mfe_r>=0.5 and net_pnl<0 then 1 else 0 end),0), "
    "coalesce(sum(be_activated),0), "
    "coalesce(sum(case when exit_source='BREAK_EVEN' then 1 else 0 end),0) "
    "from trades where book=?"
)


def summarize(path: Path = FORWARD_DB) -> dict[str, Any]:
    if not path.exists():
        return {"status": "NOT_INITIALIZED"}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exp = con.execute(
            "select candidate, baseline, started_at, forward_primary_start, be_formula "
            "from experiment where id=1"
        ).fetchone()
        health = con.execute(
            "select pid, last_tick_at, ticks, ws_connected, errors, last_poll_at, open_positions "
            "from health where id=1"
        ).fetchone()
        books: dict[str, Any] = {}
        for b in ("S0", "S1"):
            t = con.execute(_TRADE_AGG, (b,)).fetchone() or (0, 0, 0, 0, 0, 0, 0)
            opens = con.execute(
                "select count(*) from positions where book=? "
                "and json_extract(state_json,'$.exit') is null",
                (b,),
            ).fetchone()
            books[b] = {
                "completed": t[0],
                "realized_pnl": float(t[1]),
                "wins": t[2],
                "plus_1r_then_loss": t[3],
                "meaningful_reversals": t[4],
                "be_activated": t[5],
                "be_exits": t[6],
                "open": opens[0] if opens else 0,
                "equity": 100000.0 + float(t[1]),
            }
        entries = con.execute(
            "select count(*), coalesce(sum(primary_sample),0) from entries"
        ).fetchone()
        obs = [
            {"key": k, **json.loads(v)}
            for k, v in con.execute("select key, value from observation")
        ]
        return {
            "status": "SHADOW",
            "candidate": exp[0] if exp else None,
            "baseline": exp[1] if exp else None,
            "forward_since": exp[3] if exp else None,
            "be_formula": exp[4] if exp else None,
            "runner": {
                "pid": health[0],
                "last_tick_at": health[1],
                "ticks": health[2],
                "ws_connected": bool(health[3]),
                "errors": health[4],
                "last_poll_at": health[5],
                "open_positions": health[6],
            }
            if health
            else None,
            "entries_total": entries[0] if entries else 0,
            "matched_entries": entries[1] if entries else 0,
            "books": books,
            "observation_only": obs,
            "candidate_status": "UNDER_VALIDATION",
            "note": "연구용 가상 거래 — 실제 주문 없음",
        }
    finally:
        con.close()
