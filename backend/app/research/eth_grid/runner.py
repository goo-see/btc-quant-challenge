"""ETH grid SHADOW runner (launchd `com.quant.shadow-eth-grid`) — research only, zero order capability.

    public Upbit WebSocket ticker KRW-ETH  -> GridEngine.on_price (tick-level)
    data/eth_market.db (read-only, 5m)     -> daily EMA100 warm-up + 1h ATR (G3)
    data/eth_grid_shadow.db                -> the only write target (state, events, cycles, health)

Virtual sleeve ₩50,000: seed ETH ₩25,000 at the first live tick (fee + slippage applied) and ₩25,000
cash. No private client, no OrderManager, no .env secrets (tests/unit/test_eth_grid.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.types import Timeframe
from app.exchange.upbit.ws_client import Subscription, UpbitWebSocketClient
from app.market.candle_engine import CandleAggregator, resample_1m
from app.market.indicators import ATR, EMA
from app.research.eth_grid.engine import GridEngine, GridParams, grid_candidates
from app.research.round3.market_reader import MarketReader

ROOT = Path(__file__).resolve().parents[4]
ETH_DB = ROOT / "data" / "eth_market.db"
SHADOW_DB = ROOT / "data" / "eth_grid_shadow.db"
LOG = ROOT / "logs" / "shadow_eth_grid.log"
MARKET = "KRW-ETH"
SELECTED_FILE = ROOT / "docs" / "research" / "eth_grid_portfolio.json"

SCHEMA = """
create table if not exists experiment (id integer primary key check (id=1), candidate text, params text, started_at text, seed_price text, note text);
create table if not exists state (id integer primary key check (id=1), engine_json text, updated_at text);
create table if not exists events (id integer primary key autoincrement, ts text, kind text, detail text);
create table if not exists cycles (uid integer primary key, entry_time text, exit_time text, entry_price real, exit_price real, quantity real, gross real, fees real, net real, seed integer);
create table if not exists daily (day text primary key, equity real, cash real, inventory real, trades integer);
create table if not exists health (id integer primary key check (id=1), pid integer, started_at text, last_tick_at text, ticks integer, ws_connected integer, errors integer, last_error text, updated_at text);
create table if not exists runner_log (id integer primary key autoincrement, ts text, level text, message text);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def selected_candidate() -> GridParams:
    key = "G1"
    if SELECTED_FILE.exists():
        key = str(json.loads(SELECTED_FILE.read_text()).get("selected") or "G1")
    return next(c for c in grid_candidates() if c.key == key)


class EthGridShadow:
    def __init__(
        self,
        *,
        params: GridParams | None = None,
        eth_db: Path = ETH_DB,
        shadow_db: Path = SHADOW_DB,
        log_file: Path = LOG,
    ) -> None:
        import sqlite3

        self.p = params or selected_candidate()
        self.market = MarketReader(eth_db, MARKET)
        shadow_db.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(shadow_db), timeout=30)
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript(SCHEMA)
        self.log_file = log_file
        self.engine: GridEngine | None = None
        self.ema = EMA(self.p.ema_period)
        self.atr = ATR(14)
        self.agg = CandleAggregator([Timeframe.H1, Timeframe.D1], base=Timeframe.M1)
        self.m1: dict[str, Any] | None = None  # in-progress 1m bar from ticks
        self.ticks = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self.started_iso = now_iso()
        self.feed: UpbitWebSocketClient | None = None
        self._last_persist = 0.0
        self._event_cursor = 0
        self._cycle_cursor = 0

    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"{now_iso()} [{level}] {msg}"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")
        self.con.execute(
            "insert into runner_log (ts, level, message) values (?,?,?)",
            (now_iso(), level, msg[:1500]),
        )

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        bars = self.market.load(Timeframe.M5, datetime.now(UTC) - timedelta(days=200))
        for d in resample_1m(bars, Timeframe.D1):
            self.ema.update(float(d.close))
        for h in resample_1m(bars, Timeframe.H1):
            self.atr.update(float(h.high), float(h.low), float(h.close))
        row = self.con.execute("select engine_json from state where id=1").fetchone()
        if row:
            self.engine = GridEngine.from_json(self.p, json.loads(row[0]))
            self._cycle_cursor = len(self.engine.cycles)
            self.log(
                f"restored grid state: units {len(self.engine.s.units)} cash {self.engine.s.cash} realized {self.engine.s.realized} halted {self.engine.s.halted}"
            )
        else:
            self.engine = GridEngine(self.p)
            self.con.execute(
                "insert or ignore into experiment values (1,?,?,?,?,?)",
                (
                    self.p.key,
                    json.dumps(self.p.to_dict()),
                    now_iso(),
                    None,
                    "virtual sleeve 50k: seed 25k ETH at first tick + 25k cash; no real orders",
                ),
            )
            self.log(
                f"experiment created: candidate {self.p.key} ({self.p.description}); seeding at the first live tick"
            )
        if self.ema.value is not None and bars:
            last_day_close = resample_1m(bars, Timeframe.D1)[-1].close
            self.engine.s.ema = Decimal(str(self.ema.value))
            self.engine.s.trend_ok = float(last_day_close) > self.ema.value
        if self.p.gap_pct is None and self.atr.value and bars:
            self.engine.set_gap(Decimal(str(self.atr.value / float(bars[-1].close))))
        self.con.commit()

    # ------------------------------------------------------------------ ticks
    async def on_ws_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "ticker":
            return
        price = Decimal(str(msg["trade_price"]))
        raw = msg.get("trade_timestamp") or msg.get("timestamp")
        ts = (
            datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
            if isinstance(raw, int | float)
            else datetime.now(UTC)
        )
        self.on_tick(ts, price)

    def on_tick(self, ts: datetime, price: Decimal) -> None:
        assert self.engine is not None
        self.ticks += 1
        self.last_tick_at = ts
        seeded_before = self.engine.s.anchor != 0
        self.engine.on_price(ts, price)
        if not seeded_before and self.engine.s.anchor != 0:
            self.con.execute("update experiment set seed_price=? where id=1", (str(price),))
            self.log(
                f"SEEDED at {price}: units {len(self.engine.s.units)} cash {self.engine.s.cash} gap {self.engine.s.gap}"
            )
        self._aggregate(ts, price)
        if time.monotonic() - self._last_persist > 5:
            self.persist()

    def _aggregate(self, ts: datetime, price: Decimal) -> None:
        """Build 1m -> 1h/1d from ticks for the ATR (G3) and the daily EMA100 guard."""
        from app.core.types import Candle
        from app.market.candle_engine import bucket_start

        start = bucket_start(ts, Timeframe.M1)
        if self.m1 is None or self.m1["open_time"] != start:
            if self.m1 is not None:
                c = Candle(
                    MARKET,
                    Timeframe.M1,
                    self.m1["open_time"],
                    self.m1["o"],
                    self.m1["h"],
                    self.m1["l"],
                    self.m1["c"],
                    Decimal(0),
                    Decimal(0),
                )
                for closed in self.agg.add(c):
                    assert self.engine is not None
                    if closed.timeframe is Timeframe.H1:
                        a = self.atr.update(
                            float(closed.high), float(closed.low), float(closed.close)
                        )
                        if a is not None and self.p.gap_pct is None:
                            self.engine.set_gap(Decimal(str(a / float(closed.close))))
                    elif closed.timeframe is Timeframe.D1:
                        v = self.ema.update(float(closed.close))
                        self.engine.set_trend(
                            closed.close_time,
                            closed.close,
                            Decimal(str(v)) if v is not None else None,
                        )
            self.m1 = {"open_time": start, "o": price, "h": price, "l": price, "c": price}
        else:
            self.m1["h"] = max(self.m1["h"], price)
            self.m1["l"] = min(self.m1["l"], price)
            self.m1["c"] = price

    # ------------------------------------------------------------------ persistence
    def persist(self) -> None:
        assert self.engine is not None
        e = self.engine
        self.con.execute(
            "insert or replace into state values (1,?,?)", (json.dumps(e.to_json()), now_iso())
        )
        for ev in e.events[self._event_cursor :]:
            self.con.execute(
                "insert into events (ts, kind, detail) values (?,?,?)",
                (ev.ts.isoformat(), ev.kind, json.dumps(ev.detail)),
            )
            if ev.kind in (
                "BUY",
                "SELL",
                "HALT",
                "PAUSE",
                "RECENTER",
                "GUARD_ON",
                "GUARD_OFF",
                "START",
            ):
                self.log(f"{ev.kind} {json.dumps(ev.detail)}")
        self._event_cursor = len(e.events)
        for c in e.cycles[self._cycle_cursor :]:
            self.con.execute(
                "insert or replace into cycles values (?,?,?,?,?,?,?,?,?,?)",
                (
                    c.uid,
                    c.entry_time.isoformat(),
                    c.exit_time.isoformat(),
                    float(c.entry_price),
                    float(c.exit_price),
                    float(c.quantity),
                    float(c.gross),
                    float(c.fees),
                    float(c.net),
                    int(c.seed),
                ),
            )
        self._cycle_cursor = len(e.cycles)
        for day, eq in e.daily_equity[-3:]:
            self.con.execute(
                "insert or replace into daily values (?,?,?,?,?)",
                (
                    day,
                    float(eq),
                    float(e.s.cash),
                    float(e.s.inventory_value(e.s.last_price)),
                    e._daily_trades.get(day, 0),
                ),
            )
        self.con.execute(
            "insert or replace into health values (1,?,?,?,?,?,?,?,?)",
            (
                os.getpid(),
                self.started_iso,
                self.last_tick_at.isoformat() if self.last_tick_at else None,
                self.ticks,
                int(bool(self.feed and self.feed.health.connected)),
                self.errors,
                self.last_error,
                now_iso(),
            ),
        )
        self.con.commit()
        self._last_persist = time.monotonic()

    # ------------------------------------------------------------------ loops
    async def health_loop(self) -> None:
        while True:
            try:
                self.persist()
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{now_iso()} {exc!r}"
                self.log(f"persist error: {exc!r}\n{traceback.format_exc()[-800:]}", "ERROR")
            await asyncio.sleep(10)

    async def run(self) -> None:
        self.log(
            f"ETH grid shadow start pid={os.getpid()} candidate={self.p.key} (public WS ticker; eth_market.db read-only; no order path)"
        )
        self.bootstrap()
        self.feed = UpbitWebSocketClient(
            [Subscription("ticker", [MARKET])], self.on_ws_message, ping_interval=30.0
        )
        await asyncio.gather(self.feed.run(), self.health_loop())


def main() -> int:
    asyncio.run(EthGridShadow().run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
