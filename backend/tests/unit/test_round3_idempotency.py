"""Round 3: research store idempotency — replaying the same bars never duplicates rows."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from app.core.types import Candle, Timeframe
from app.market.candle_engine import resample_1m
from app.research.round3.config import DEFAULT_CONFIG, load_config
from app.research.round3.runner import backtest_config
from app.research.round3.shadow_engine import ShadowEngine
from app.research.round3.store import ResearchStore
from tests.unit.test_round3_shadow_engine import T0, synthetic_1m


def _replay(store: ResearchStore, sid: str, h1: list[Candle], replay: bool) -> dict[str, int]:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy(sid)
    eng = ShadowEngine(
        sd.build(),
        backtest_config(100_000),
        start_at=T0 + timedelta(days=60),
        base=Timeframe.H1,
        strategy_id=sid,
    )
    counts = {"decisions": 0, "fills": 0, "trades": 0}
    for c in h1:
        ev = eng.on_bar(c)
        for r in ev.rejects:
            store.add_reject(sd, r.signal_bar_close, r.reason)
        for f in ev.fills:
            counts["fills"] += int(store.add_fill(sd, f))
        for t in ev.trades:
            counts["trades"] += int(store.add_trade(sd, t))
        for d in ev.decisions:
            counts["decisions"] += int(store.add_decision(cfg, sd, d, replay=replay))
    store.save_portfolio(sd, eng.snapshot())
    store.commit()
    return counts


def _table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        return {
            t: con.execute(f"select count(*) from {t}").fetchone()[0]
            for t in (
                "decisions",
                "fills",
                "trades",
                "forward_tracking",
                "portfolio",
                "divergences",
            )
        }
    finally:
        con.close()


def test_replay_inserts_nothing_new_and_no_divergence(tmp_path: Path) -> None:
    db = tmp_path / "r3.db"
    h1 = resample_1m(synthetic_1m(160), Timeframe.H1)
    store = ResearchStore(db)
    first = _replay(store, "D1", h1, replay=False)
    assert first["decisions"] > 0 and first["fills"] > 0 and first["trades"] > 0
    c1 = _table_counts(db)
    second = _replay(store, "D1", h1, replay=True)  # restart -> deterministic replay
    assert second == {"decisions": 0, "fills": 0, "trades": 0}
    c2 = _table_counts(db)
    assert c1 == c2
    assert c2["divergences"] == 0
    assert c2["portfolio"] == 1
    # unique key (strategy_id, market_bar_time) is enforced at the schema level as well
    con = sqlite3.connect(db)
    n = con.execute(
        "select count(*) from (select strategy_id, market_bar_time, count(*) c from decisions group by 1,2 having c > 1)"
    ).fetchone()[0]
    assert n == 0
    # every BUY fill opened exactly one forward-tracking row; every closed trade closed one
    buys = con.execute("select count(*) from fills where side='BUY'").fetchone()[0]
    assert con.execute("select count(*) from forward_tracking").fetchone()[0] == buys
    closed = con.execute("select count(*) from forward_tracking where status='CLOSED'").fetchone()[
        0
    ]
    assert closed == c2["trades"]
    # decisions that produced fills were updated from PENDING_NEXT_BAR to *_FILLED
    assert (
        con.execute(
            "select count(*) from decisions where virtual_execution='PENDING_NEXT_BAR'"
        ).fetchone()[0]
        <= 1
    )
    assert (
        con.execute(
            "select count(*) from decisions where virtual_execution='BUY_FILLED'"
        ).fetchone()[0]
        == buys
    )
    con.close()
    store.close()


def test_divergent_replay_is_logged_not_overwritten(tmp_path: Path) -> None:
    db = tmp_path / "r3.db"
    h1 = resample_1m(synthetic_1m(120), Timeframe.H1)
    store = ResearchStore(db)
    _replay(store, "D2", h1, replay=False)
    # tamper the market data (a revised candle) and replay -> stored rows win, divergence recorded
    tampered = (
        [
            c.__class__(**{**c.__dict__, "close": c.close * 2}) if i == 2000 else c
            for i, c in enumerate(h1)
        ]
        if hasattr(h1[0], "__dict__")
        else h1
    )
    from dataclasses import replace

    tampered = [
        replace(c, close=c.close * 2, high=c.high * 2) if i == 2000 else c for i, c in enumerate(h1)
    ]
    _replay(store, "D2", tampered, replay=True)
    con = sqlite3.connect(db)
    div = con.execute("select count(*) from divergences").fetchone()[0]
    assert div >= 1
    stored = con.execute(
        "select timeframe_close from decisions where strategy_id='D2' and market_bar_time=?",
        (h1[2000].close_time.isoformat(),),
    ).fetchone()
    assert (
        stored is not None and abs(float(stored[0]) - float(h1[2000].close)) < 1e-6
    )  # first write wins
    con.close()
    store.close()
