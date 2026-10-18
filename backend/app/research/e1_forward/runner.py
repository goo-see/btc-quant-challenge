"""E1 forward shadow runner (launchd `com.quant.shadow-e1`) — research only, zero order capability.

    production quant.db (sqlite ?mode=ro)  ->  confirmed LIVE D0 BUY entries, LIVE trailing stop,
                                                LIVE SELL decisions, 1m candles (catch-up)
    Upbit PUBLIC WebSocket ticker          ->  tick-level stop / +1R / break-even processing
    data/d0_v2_e1_forward.db               ->  the only write target

No private client, no OrderManager, no RiskManager, no .env secrets (test_e1_forward.py).
"""

from __future__ import annotations

import asyncio
import contextlib
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
from app.research.e1_forward.books import (
    FEE,
    SLIP,
    BookState,
    Event,
    break_even_stop,
    open_pair,
    virtual_buy,
)
from app.research.e1_forward.store import ForwardStore, now_iso
from app.research.round3.market_reader import MarketReader

ROOT = Path(__file__).resolve().parents[4]
MARKET_DB = ROOT / "data" / "quant.db"
FORWARD_DB = ROOT / "data" / "d0_v2_e1_forward.db"
LOG = ROOT / "logs" / "shadow_e1.log"
MARKET = "KRW-BTC"
BE_FORMULA = "be_stop = ceil(entry_fill * (1 + fee) / ((1 - slip) * (1 - fee))); R = entry_fill - initial_stop; trigger = entry_fill + 1R (highest tick)"


def git_head() -> str:
    try:
        head = (ROOT / ".git" / "HEAD").read_text().strip()
        return (ROOT / ".git" / head[5:]).read_text().strip() if head.startswith("ref: ") else head
    except OSError:
        return "unknown"


class E1ForwardRunner:
    def __init__(
        self,
        *,
        market_db: Path = MARKET_DB,
        forward_db: Path = FORWARD_DB,
        log_file: Path = LOG,
        poll_sec: float = 5.0,
    ) -> None:
        self.market = MarketReader(market_db, MARKET)
        self.store = ForwardStore(forward_db)
        self.log_file = log_file
        self.poll_sec = poll_sec
        self.books: dict[tuple[str, int], BookState] = {}
        self.ticks = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self.started_iso = now_iso()
        self.feed: UpbitWebSocketClient | None = None
        self._seq = 0
        self._dirty: set[tuple[str, int]] = set()
        self._last_persist = 0.0

    # ------------------------------------------------------------------ logging
    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"{now_iso()} [{level}] {msg}"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")
        self.store.log(level, msg)

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        created = self.store.ensure_experiment(
            candidate="E1_BREAK_EVEN_1R",
            baseline="S0_D0_V1",
            started_at=now_iso(),
            forward_primary_start=now_iso(),
            be_formula=BE_FORMULA,
            fee=str(FEE),
            slip=str(SLIP),
            git_head=git_head(),
            notes="entries = confirmed LIVE D0 BUY fills (read-only); S0 mirrors LIVE trailing/SELL; S1 adds BE at +1R; current LIVE trade at start = OBSERVATION_ONLY",
        )
        exp = self.store.experiment() or {}
        self.primary_start = datetime.fromisoformat(str(exp["forward_primary_start"]))
        if created:
            self.log(
                f"experiment created: primary forward start {self.primary_start.isoformat()}; BE formula: {BE_FORMULA}"
            )
            self._record_observation_only_live_trade()
        for st in self.store.load_positions():
            self.books[(st.book, st.entry_id)] = st
        restored = sum(1 for b in self.books.values() if b.open)
        self.log(
            f"restored {len(self.books)} book states ({restored} open; BE active on {sum(1 for b in self.books.values() if b.open and b.be_activated)})"
        )
        self.catch_up()
        self.store.commit()

    def _record_observation_only_live_trade(self) -> None:
        """The LIVE position open at start is NOT a primary sample (its ticks were not tracked)."""
        rows = self.market.connection.execute(
            "select id, client_order_id, signal_id, avg_fill_price, executed_volume, created_at from orders where mode='LIVE' and side='bid' and executed_volume>0 order by id"
        ).fetchall()
        pf = json.loads(self.market.bot_state("portfolio") or "{}")
        st = pf.get("state", {})
        for oid, cid, sid, px, qty, created in rows:
            em = self.market.connection.execute(
                "select initial_stop, signal_price, atr from execution_metrics where client_order_id=?",
                (cid,),
            ).fetchone()
            init_stop = Decimal(str(em[0])) if em and em[0] is not None else None
            entry = Decimal(str(px))
            r = (entry - init_stop) if init_stop is not None else None
            hi = Decimal(str(st.get("highest_since_entry") or 0))
            self.store.set_observation(
                f"live_order_{oid}",
                {
                    "status": "OBSERVATION_ONLY_CURRENT_LIVE_TRADE",
                    "live_order_id": oid,
                    "client_order_id": cid,
                    "signal_id": sid,
                    "entry_fill": str(entry),
                    "quantity": str(qty),
                    "entry_time": str(created),
                    "initial_stop": str(init_stop) if init_stop is not None else None,
                    "r_unit": str(r) if r is not None else None,
                    "hypothetical_1r_trigger": str(entry + r) if r is not None else None,
                    "hypothetical_be_stop": str(break_even_stop(entry)),
                    "highest_since_entry": str(hi),
                    "current_mfe_r": str((hi - entry) / r) if (r and hi) else None,
                    "live_current_stop": pf.get("stop_price"),
                    "note": "excluded from primary forward statistics (not tick-tracked from entry)",
                },
            )
            # also mark the order id as consumed so it is never turned into a primary entry
            if not self.store.has_entry(int(oid)):
                self.store.add_entry(
                    {
                        "live_order_id": int(oid),
                        "live_client_order_id": cid,
                        "signal_id": sid,
                        "signal_time": str(created),
                        "signal_price": None,
                        "initial_stop": float(init_stop) if init_stop is not None else None,
                        "krw_amount": None,
                        "virtual_fill_time": None,
                        "virtual_fill_ref": None,
                        "virtual_fill_price": None,
                        "quantity": float(qty),
                        "entry_fee": None,
                        "r_unit": float(r) if r is not None else None,
                        "be_trigger": None,
                        "be_stop": None,
                        "live_fill_price": float(px),
                        "live_quantity": float(qty),
                        "primary_sample": 0,
                        "created_at": now_iso(),
                    }
                )
            self.log(
                f"OBSERVATION_ONLY live order {oid}: entry {entry} initial_stop {init_stop} R {r} +1R {entry + r if r else '—'} (excluded from primary sample)"
            )

    # ------------------------------------------------------------------ entries
    def poll_entries(self) -> None:
        rows = self.market.connection.execute(
            "select id, client_order_id, signal_id, req_krw, avg_fill_price, executed_volume, created_at from orders where mode='LIVE' and side='bid' and executed_volume>0 order by id"
        ).fetchall()
        for oid, cid, sid, req_krw, px, qty, _created in rows:
            if self.store.has_entry(int(oid)):
                continue
            sig = self.market.connection.execute(
                "select ts, stop_price, json_extract(indicators,'$.close') from signals where id=?",
                (sid,),
            ).fetchone()
            if not sig or sig[1] is None:
                self.log(f"entry {oid}: signal {sid} missing stop -> skipped", "WARNING")
                continue
            sig_ts = datetime.fromisoformat(str(sig[0])).replace(tzinfo=UTC)
            init_stop = Decimal(str(sig[1]))
            # virtual fill = first 1m open at/after the signal close (same rule as the backtests)
            bars = self.market.load(Timeframe.M1, sig_ts, sig_ts + timedelta(minutes=10))
            if not bars:
                self.log(f"entry {oid}: no 1m bar after {sig_ts} yet", "WARNING")
                continue
            fill = virtual_buy(bars[0].open_time, bars[0].open, Decimal(str(req_krw or 30000)))
            entry_id = self.store.add_entry(
                {
                    "live_order_id": int(oid),
                    "live_client_order_id": cid,
                    "signal_id": sid,
                    "signal_time": sig_ts.isoformat(),
                    "signal_price": float(sig[2]) if sig[2] is not None else None,
                    "initial_stop": float(init_stop),
                    "krw_amount": float(req_krw or 0),
                    "virtual_fill_time": fill.ts.isoformat(),
                    "virtual_fill_ref": float(fill.ref_price),
                    "virtual_fill_price": float(fill.price),
                    "quantity": float(fill.quantity),
                    "entry_fee": float(fill.fee),
                    "r_unit": float(fill.price - init_stop),
                    "be_trigger": float(fill.price * 2 - init_stop),
                    "be_stop": float(break_even_stop(fill.price)),
                    "live_fill_price": float(px),
                    "live_quantity": float(qty),
                    "primary_sample": 1,
                    "created_at": now_iso(),
                }
            )
            s0, s1 = open_pair(entry_id, fill, init_stop)
            for st in (s0, s1):
                self.books[(st.book, entry_id)] = st
                self.store.save_position(st)
                self.store.add_event(
                    Event(
                        fill.ts,
                        "ENTRY",
                        st.book,
                        {
                            "fill": str(fill.price),
                            "qty": str(fill.quantity),
                            "initial_stop": str(init_stop),
                            "r": str(st.r_unit),
                            "be_trigger": str(st.be_trigger),
                            "be_stop": str(st.be_stop),
                            "live_fill": str(px),
                        },
                    ),
                    entry_id,
                )
            self.log(
                f"NEW PRIMARY ENTRY #{entry_id} (live order {oid}): fill {fill.price:.0f} x {fill.quantity} (live {px}), initial stop {init_stop:.0f}, R {s0.r_unit:.0f}, +1R {s1.be_trigger:.0f}, BE stop {s1.be_stop:.0f}"
            )
            # catch up ticks that already happened since the fill (closed 1m bars, pessimistic)
            for bar in self.market.load(
                Timeframe.M1,
                fill.ts + timedelta(minutes=1),
                datetime.now(UTC) - timedelta(minutes=1),
            ):
                for st in (s0, s1):
                    self._handle_events(st, st.on_catchup_bar(bar))
        self.store.commit()

    # ------------------------------------------------------------------ LIVE mirroring
    def poll_live_state(self) -> None:
        pf = json.loads(self.market.bot_state("portfolio") or "{}")
        live_stop = Decimal(str(pf["stop_price"])) if pf.get("stop_price") else None
        live_has_pos = Decimal(str(pf.get("state", {}).get("btc", 0))) > 0
        now = datetime.now(UTC)
        for st in self.open_books():
            if live_stop is not None and live_has_pos:
                ev = st.apply_trailing(live_stop, now)
                if ev:
                    self.store.add_event(ev, st.entry_id)
                    self.store.save_position(st)
        # LIVE SELL decisions (channel exit / trend loss) -> mirrored exit at the next 1m open
        if self.open_books():
            latest_entry_time = min(st.entry_time for st in self.open_books())
            sells = self.market.connection.execute(
                "select id, ts, reason, json_extract(indicators,'$.close') from signals where mode='LIVE' and action='SELL' and ts > ? order by id",
                (latest_entry_time.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" "),),
            ).fetchall()
            for sid, ts, reason, _close in sells:
                if str(reason).startswith("STOP"):
                    continue  # tick stops are handled by the shadow's own tick path
                sig_ts = datetime.fromisoformat(str(ts)).replace(tzinfo=UTC)
                bars = self.market.load(Timeframe.M1, sig_ts, sig_ts + timedelta(minutes=10))
                if not bars:
                    continue
                for st in self.open_books():
                    if st.entry_time <= sig_ts:
                        ev = st.on_live_sell(
                            bars[0].open_time, bars[0].open, f"{reason} (LIVE signal {sid})"
                        )
                        if ev:
                            self._handle_events(st, [ev])
        self.store.commit()

    def open_books(self) -> list[BookState]:
        return [b for b in self.books.values() if b.open]

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
        self.ticks += 1
        self._seq += 1
        self.last_tick_at = ts
        for st in self.open_books():
            evs = st.on_tick(ts, price, seq=self._seq)
            self._dirty.add((st.book, st.entry_id))
            if evs:
                self._handle_events(st, evs)
        if time.monotonic() - self._last_persist > 5:
            self._persist_dirty()

    def _persist_dirty(self) -> None:
        for key in list(self._dirty):
            st = self.books.get(key)
            if st:
                self.store.save_position(st)
        self._dirty.clear()
        self._last_persist = time.monotonic()
        with contextlib.suppress(Exception):
            self.store.commit()

    def _handle_events(self, st: BookState, evs: list[Event]) -> None:
        for e in evs:
            self.store.add_event(e, st.entry_id)
            self.log(f"{st.book} #{st.entry_id} {e.kind} {json.dumps(e.detail, sort_keys=True)}")
        self.store.save_position(st)
        if not st.open:
            self.store.add_trade(st)
            if st.book == "S1" and st.exit_source == "BREAK_EVEN":
                self.store.con.execute(
                    "insert or ignore into be_followups (entry_id, exit_time, exit_price, updated_at) values (?,?,?,?)",
                    (
                        st.entry_id,
                        st.exit.ts.isoformat() if st.exit else None,
                        float(st.exit.price) if st.exit else None,
                        now_iso(),
                    ),
                )
        self.store.commit()

    # ------------------------------------------------------------------ catch-up & follow-ups
    def catch_up(self) -> None:
        """After a restart: replay closed 1m bars since each open book's last tick (pessimistic)."""
        cutoff = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
        for st in self.open_books():
            since = (st.last_tick_at or st.entry_time).replace(second=0, microsecond=0) + timedelta(
                minutes=1
            )
            bars = self.market.load(Timeframe.M1, since, cutoff)
            n = 0
            for bar in bars:
                evs = st.on_catchup_bar(bar)
                n += 1
                if evs:
                    self._handle_events(st, evs)
                if not st.open:
                    break
            if n:
                self.store.save_position(st)
                self.log(
                    f"catch-up {st.book} #{st.entry_id}: {n} closed 1m bars (LOW-before-HIGH, pessimistic)"
                )

    def update_followups(self) -> None:
        now = datetime.now(UTC)
        for eid, exit_time, exit_price, r4, r8, r12, r24, r48, r72 in self.store.q(
            "select entry_id, exit_time, exit_price, ret_4h, ret_8h, ret_12h, ret_24h, ret_48h, ret_72h from be_followups where ret_72h is null or s0_final_pnl is null"
        ):
            et = datetime.fromisoformat(exit_time)
            px0 = Decimal(str(exit_price))
            vals: dict[str, float | None] = {}
            for h, cur in ((4, r4), (8, r8), (12, r12), (24, r24), (48, r48), (72, r72)):
                if cur is not None:
                    vals[f"ret_{h}h"] = float(cur)
                    continue
                at = et + timedelta(hours=h)
                px = (
                    self.market.close_at(
                        Timeframe.M1, at.replace(second=0, microsecond=0) - timedelta(minutes=1)
                    )
                    if at <= now - timedelta(minutes=2)
                    else None
                )
                vals[f"ret_{h}h"] = float((px - px0) / px0 * 100) if px is not None else None
            s0 = self.store.q(
                "select net_pnl, exit_time, exit_reason from trades where book='S0' and entry_id=?",
                (eid,),
            )
            self.store.con.execute(
                "update be_followups set ret_4h=?, ret_8h=?, ret_12h=?, ret_24h=?, ret_48h=?, ret_72h=?, s0_final_pnl=?, s0_exit_time=?, s0_exit_reason=?, updated_at=? where entry_id=?",
                (
                    vals["ret_4h"],
                    vals["ret_8h"],
                    vals["ret_12h"],
                    vals["ret_24h"],
                    vals["ret_48h"],
                    vals["ret_72h"],
                    s0[0][0] if s0 else None,
                    s0[0][1] if s0 else None,
                    s0[0][2] if s0 else None,
                    now_iso(),
                    eid,
                ),
            )
        self.store.commit()

    # ------------------------------------------------------------------ loops
    async def poll_loop(self) -> None:
        while True:
            try:
                self.poll_entries()
                self.poll_live_state()
                self.update_followups()
                self._persist_dirty()
                self.store.health(
                    pid=os.getpid(),
                    started_at=self.started_iso,
                    last_tick_at=self.last_tick_at.isoformat() if self.last_tick_at else None,
                    ticks=self.ticks,
                    ws_connected=bool(self.feed and self.feed.health.connected),
                    errors=self.errors,
                    last_error=self.last_error,
                    open_positions=len(self.open_books()),
                )
                self.store.commit()
            except Exception as exc:  # never die; LIVE is a different process anyway
                self.errors += 1
                self.last_error = f"{now_iso()} {exc!r}"
                self.log(f"poll error: {exc!r}\n{traceback.format_exc()[-1200:]}", "ERROR")
            await asyncio.sleep(self.poll_sec)

    async def run(self) -> None:
        self.log(
            f"E1 forward shadow start pid={os.getpid()} (public WS ticker only; market DB read-only; no order path)"
        )
        self.bootstrap()
        self.feed = UpbitWebSocketClient(
            [Subscription("ticker", [MARKET])], self.on_ws_message, ping_interval=30.0
        )
        await asyncio.gather(self.feed.run(), self.poll_loop())


def main() -> int:
    asyncio.run(E1ForwardRunner().run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
