"""Run PAPER trading against live Upbit data for a fixed duration (or until Ctrl-C).

usage: TRADING_MODE=PAPER uv run python ../scripts/run_paper.py [--seconds 300] [--reset]
--reset clears persisted PAPER state (bot/portfolio/gateway) before starting.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.core.logging import configure_logging
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.main_trader import run


async def _reset(settings: Settings) -> None:
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    for key in ("bot", "portfolio", "gateway", "kill_switch"):
        await repo.set_state(key, {})
    from sqlalchemy import delete

    from app.database.engine import session_scope
    from app.database.models import BotStateRow

    async with session_scope(repo._factory) as s:
        await s.execute(delete(BotStateRow))
    await engine.dispose()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("TRADING_MODE", "PAPER")
    settings = Settings.load()
    if settings.mode is not TradingMode.PAPER:
        raise SystemExit(f"run_paper requires TRADING_MODE=PAPER (got {settings.mode})")
    configure_logging(
        settings.config.system.log_level, settings.config.system.log_dir, json_output=False
    )
    if args.reset:
        await _reset(settings)
    svc = await run(settings, duration_sec=args.seconds)
    pf, gw = svc.portfolio, svc.gateway
    ws = svc.ws_health()
    print("\nPAPER RUN SUMMARY")
    print(f"  run_id={svc.run_id} state={svc.sm.state} reason={svc.sm.reason}")
    print(
        f"  ws: connected={ws.connected} msgs={ws.message_count} reconnects={ws.reconnect_count} latency={ws.latency_ema_ms:.0f}ms"
    )
    print(
        f"  data_age={svc.data_age()} last_signal={svc.last_signal.action if svc.last_signal else None} ({svc.last_signal.reason if svc.last_signal else ''})"
    )
    print(f"  indicators ready: { {tf.value: i.ready for tf, i in svc.indicators.items()} }")
    price = pf.last_price
    if price is not None:
        print(
            f"  price={price} equity={pf.equity()} krw={pf.state.krw} btc={pf.state.btc} daily_pnl={pf.daily_pnl()} dd={pf.drawdown():.4f}"
        )
    print(f"  risk approved={svc.risk.approved} rejected={svc.risk.rejected}")
    print(
        f"  orders submitted={svc.orders.submitted} filled={svc.orders.filled} failed={svc.orders.failed} dup_blocked={svc.orders.duplicates_blocked}"
    )
    print(
        f"  reconcile last={svc.reconciler.last.status if svc.reconciler.last else None} recovery={svc.recovery.last.failed if svc.recovery.last else None}"
    )
    print(f"  gateway ledger={getattr(gw, 'ledger', None)}")


if __name__ == "__main__":
    asyncio.run(main())
