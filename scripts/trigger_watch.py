"""Pure local watcher for the D PAPER validation — NO Claude/LLM/network calls except 127.0.0.1.

Every 60s: read the local SQLite DB + http://127.0.0.1:8787/api/readiness, and append ONE line per
trigger to logs/trigger_watch.log (state in logs/trigger_watch.state):
  1 first D BUY fill   2 first D SELL fill   3 CRITICAL event   4 reconciliation FAIL
  5 static IP 24h PASS 6 PAPER runtime 24h PASS   (+ health FAIL / API unreachable, hourly at most)
Run by launchd (com.quant.watch) with the backend venv python; restarts on exit.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
LOG = ROOT / "logs" / "trigger_watch.log"
STATE = ROOT / "logs" / "trigger_watch.state"
API = "http://127.0.0.1:8787/api/readiness"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fired(key: str) -> bool:
    return STATE.exists() and key in STATE.read_text().splitlines()


def fire(key: str, msg: str) -> None:
    with STATE.open("a") as f:
        f.write(key + "\n")
    line = f"{now()} [TRIGGER] {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def q1(sql: str) -> str | None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(sql).fetchone()
        return None if row is None or row[0] is None else str(row[0])
    finally:
        con.close()


def readiness() -> dict | None:  # type: ignore[type-arg]
    try:
        with urllib.request.urlopen(API, timeout=10) as r:  # noqa: S310 — loopback only
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def items(d: dict, group: str) -> dict[str, dict]:  # type: ignore[type-arg]
    g = next((g for g in d["groups"] if g["name"] == group), None)
    return {i["name"]: i for i in (g["items"] if g else [])}


def tick() -> None:
    epoch = q1("select json_extract(value,'$.started_at') from bot_state where key='strategy_epoch_PAPER'")
    ver = q1("select json_extract(value,'$.strategy_version') from bot_state where key='strategy_epoch_PAPER'") or "?"
    if not epoch:
        return
    buy = q1(f"select client_order_id||' '||executed_volume||' BTC @ '||round(avg_fill_price)||' '||created_at from orders where mode='PAPER' and side='bid' and state='FILLED' and created_at > '{epoch}' order by id limit 1")
    if buy and not fired("buy"):
        fire("buy", f"1 first natural BUY ({ver}): {buy}")
    sell = q1(f"select client_order_id||' '||executed_volume||' BTC @ '||round(avg_fill_price)||' '||created_at from orders where mode='PAPER' and side='ask' and state in ('FILLED','CANCELLED') and executed_volume>0 and created_at > '{epoch}' order by id limit 1")
    if sell and not fired("sell"):
        pnl = q1(f"select net_pnl||' KRW '||exit_reason from trades where mode='PAPER' and status='CLOSED' and exit_time > '{epoch}' order by id desc limit 1")
        fire("sell", f"2 first natural SELL ({ver}): {sell} | trade {pnl}")
    crit = q1(f"select ts||' '||type||' '||substr(message,1,120) from events where level='CRITICAL' and ts > '{epoch}' order by id desc limit 1")
    if crit and not fired("crit:" + crit[:19]):
        fire("crit:" + crit[:19], f"3 CRITICAL: {crit}")
    rfail = q1(f"select ts||' krw_diff='||krw_diff||' btc_diff='||btc_diff from reconciliations where status='FAIL' and ts > '{epoch}' order by id desc limit 1")
    if rfail and not fired("rfail:" + rfail[:19]):
        fire("rfail:" + rfail[:19], f"4 RECONCILIATION FAIL: {rfail}")
    d = readiness()
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    if d is None:
        if not fired("api:" + hour):
            fire("api:" + hour, "API unreachable (readiness)")
        return
    infra, strat = items(d, "Infrastructure"), items(d, "Strategy PAPER")
    ip = infra.get("Static public IP", {})
    if ip.get("status") == "PASS" and not fired("ip24"):
        fire("ip24", f"5 STATIC IP 24h: {ip.get('value')} — {ip.get('detail')}")
    rt = strat.get("Strategy runtime", {})
    if rt.get("status") == "PASS" and not fired("rt24"):
        ready = strat.get("paper_approval_ready", {}).get("value")
        fire("rt24", f"6 PAPER RUNTIME 24h ({ver}): {rt.get('value')} | paper_approval_ready={ready}")
    bad = [k for k in ("WebSocket", "REST", "Database") if strat.get(k, {}).get("status") == "FAIL"]
    if bad and not fired("health:" + hour):
        fire("health:" + hour, f"HEALTH FAIL: {bad}")


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{now()} watcher start (python, loopback-only, no LLM) pid={__import__('os').getpid()}\n")
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — never die on a transient error
            with LOG.open("a") as f:
                f.write(f"{now()} watcher error: {exc!r}\n")
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
