"""Static public IPv4 verification procedure for the Upbit API-key IP whitelist.

usage: uv run python ../scripts/check_public_ip.py            # observe once, append to history, show verdict
       uv run python ../scripts/check_public_ip.py --history  # show stored observations only

Rules: only the OUTBOUND PUBLIC IPv4 counts. Tailscale 100.x, LAN 172/192/10 and loopback are
rejected. The gate passes when config live_gate.static_public_ip equals every observation and the
observations span >= live_gate.static_ip_min_observation_hours (default 24h, hourly by the trader).
"""

from __future__ import annotations

import argparse
import asyncio

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.monitoring.network import append_observation, evaluate_static_ip, observe_public_ipv4


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    settings = Settings.load()
    cfg = settings.config.live_gate
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    state = await repo.get_state("public_ip") or {}
    history = list(state.get("history", []))
    if not args.history:
        ip, detail = await observe_public_ipv4()
        print("sources:")
        for url, v in detail.items():
            print(f"  {url:<34} {v}")
        print(f"observed public IPv4: {ip or 'INCONSISTENT / NONE'}")
        history = append_observation(history, ip, detail)
        await repo.set_state(
            "public_ip", {"history": history, "latest": ip, "at": history[-1]["at"]}
        )
    print(f"\nobservations: {len(history)}")
    for h in history[-10:]:
        print(f"  {h['at'][:19]}  {h['ip']}")
    v = evaluate_static_ip(
        cfg.static_public_ip or None, history, min_hours=cfg.static_ip_min_observation_hours
    )
    print(
        f"\nconfigured static_public_ip: {v.configured or '(not set — edit config/config.yaml live_gate.static_public_ip)'}"
    )
    print(
        f"verdict: {'PASS' if v.passed else 'FAIL'} — {v.reason} (span {v.span_hours:.1f}h, ips {v.observed_ips})"
    )
    print(
        "\nNext: register this IPv4 (NOT the Tailscale 100.x or LAN address) in the Upbit Open API key"
    )
    print("      IP whitelist with permissions [자산조회, 주문하기] only. Never grant withdrawal.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
