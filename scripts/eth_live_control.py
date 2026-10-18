"""ETH LIVE EXPERIMENT control switch (the ONLY way the grid process is allowed to start ordering).

usage: cd backend && uv run python ../scripts/eth_live_control.py enable --by "user approval 2026-10-05"
       ... disable | pause-buys --reason PAUSED_NEW_BUYS_INVENTORY_RISK | resume-buys | unfreeze --by "operator" | show
Writes bot_state.eth_grid_control in the grid process's own DB; never places an order itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.eth_grid.service import CONTROL_KEY, STATE_KEY


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "action", choices=["enable", "disable", "unfreeze", "pause-buys", "resume-buys", "show"]
    )
    ap.add_argument("--by", default="operator")
    ap.add_argument("--reason", default="PAUSED_NEW_BUYS")
    args = ap.parse_args()
    s = Settings.load()
    engine = create_engine(s.config.eth_grid.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    ctl = await repo.get_state(CONTROL_KEY) or {}
    now = datetime.now(UTC).isoformat()
    if args.action == "enable":
        ctl.update({"enabled": True, "enabled_at": now, "enabled_by": args.by})
        await repo.set_state(CONTROL_KEY, ctl)
    elif args.action == "disable":
        ctl.update({"enabled": False, "disabled_at": now, "disabled_by": args.by})
        await repo.set_state(CONTROL_KEY, ctl)
    elif args.action == "pause-buys":
        # NEW buys only: sells of held lots, lots, targets, sizing state are untouched; no liquidation
        ctl.update(
            {
                "buy_paused": True,
                "buy_pause_reason": args.reason,
                "buy_pause_by": args.by,
                "buy_paused_at": now,
            }
        )
        await repo.set_state(CONTROL_KEY, ctl)
    elif args.action == "resume-buys":
        ctl.update({"buy_paused": False, "buy_resumed_at": now, "buy_resume_by": args.by})
        await repo.set_state(CONTROL_KEY, ctl)
    elif args.action == "unfreeze":
        ctl.update({"unfreeze_request_at": now, "unfreeze_by": args.by})
        await repo.set_state(CONTROL_KEY, ctl)
    st = await repo.get_state(STATE_KEY) or {}
    print("control:", json.dumps(await repo.get_state(CONTROL_KEY) or {}, ensure_ascii=False))
    print(
        "state:",
        json.dumps(
            {
                k: st.get(k)
                for k in ("status", "cash", "anchor", "frozen", "frozen_reason", "updated_at")
            },
            ensure_ascii=False,
        ),
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
