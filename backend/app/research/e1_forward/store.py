"""Forward DB (data/d0_v2_e1_forward.db): the ONLY write target of the E1 shadow runner."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.research.e1_forward.books import BookState, Event

SCHEMA = """
create table if not exists experiment (id integer primary key check (id=1), candidate text, baseline text, started_at text, forward_primary_start text, be_formula text, fee text, slip text, git_head text, notes text);
create table if not exists entries (entry_id integer primary key, live_order_id integer unique, live_client_order_id text, signal_id integer, signal_time text, signal_price real, initial_stop real, krw_amount real, virtual_fill_time text, virtual_fill_ref real, virtual_fill_price real, quantity real, entry_fee real, r_unit real, be_trigger real, be_stop real, live_fill_price real, live_quantity real, primary_sample integer, created_at text);
create table if not exists positions (book text, entry_id integer, state_json text, updated_at text, primary key (book, entry_id));
create table if not exists events (id integer primary key autoincrement, ts text, book text, entry_id integer, kind text, detail text, unique(ts, book, entry_id, kind, detail));
create table if not exists trades (book text, entry_id integer, entry_time text, entry_price real, quantity real, exit_time text, exit_price real, exit_reason text, exit_source text, net_pnl real, return_pct real, fees real, holding_sec integer, mfe_r real, mae_r real, be_activated integer, primary key (book, entry_id));
create table if not exists be_followups (entry_id integer primary key, exit_time text, exit_price real, ret_4h real, ret_8h real, ret_12h real, ret_24h real, ret_48h real, ret_72h real, s0_final_pnl real, s0_exit_time text, s0_exit_reason text, updated_at text);
create table if not exists observation (key text primary key, value text, updated_at text);
create table if not exists health (id integer primary key check (id=1), pid integer, started_at text, last_tick_at text, ticks integer, ws_connected integer, errors integer, last_error text, last_poll_at text, open_positions integer);
create table if not exists runner_log (id integer primary key autoincrement, ts text, level text, message text);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ForwardStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(str(path), timeout=30)
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def commit(self) -> None:
        self.con.commit()

    def q(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, args).fetchall()

    def log(self, level: str, msg: str) -> None:
        self.con.execute(
            "insert into runner_log (ts, level, message) values (?,?,?)",
            (now_iso(), level, msg[:2000]),
        )

    def ensure_experiment(self, **kw: Any) -> bool:
        if self.q("select 1 from experiment where id=1"):
            return False
        self.con.execute(
            "insert into experiment values (1,?,?,?,?,?,?,?,?,?)",
            (
                kw["candidate"],
                kw["baseline"],
                kw["started_at"],
                kw["forward_primary_start"],
                kw["be_formula"],
                kw["fee"],
                kw["slip"],
                kw["git_head"],
                kw.get("notes", ""),
            ),
        )
        self.con.commit()
        return True

    def experiment(self) -> dict[str, Any] | None:
        r = self.q(
            "select candidate, baseline, started_at, forward_primary_start, be_formula, fee, slip, git_head, notes from experiment where id=1"
        )
        if not r:
            return None
        k = [
            "candidate",
            "baseline",
            "started_at",
            "forward_primary_start",
            "be_formula",
            "fee",
            "slip",
            "git_head",
            "notes",
        ]
        return dict(zip(k, r[0], strict=True))

    def has_entry(self, live_order_id: int) -> bool:
        return bool(self.q("select 1 from entries where live_order_id=?", (live_order_id,)))

    def add_entry(self, row: dict[str, Any]) -> int:
        cols = [
            "live_order_id",
            "live_client_order_id",
            "signal_id",
            "signal_time",
            "signal_price",
            "initial_stop",
            "krw_amount",
            "virtual_fill_time",
            "virtual_fill_ref",
            "virtual_fill_price",
            "quantity",
            "entry_fee",
            "r_unit",
            "be_trigger",
            "be_stop",
            "live_fill_price",
            "live_quantity",
            "primary_sample",
            "created_at",
        ]
        sql = f"insert into entries ({','.join(cols)}) values ({','.join('?' * len(cols))})"  # noqa: S608 — constant column list
        cur = self.con.execute(
            sql,
            tuple(row.get(c) for c in cols),
        )
        return int(cur.lastrowid or 0)

    def save_position(self, st: BookState) -> None:
        self.con.execute(
            "insert or replace into positions (book, entry_id, state_json, updated_at) values (?,?,?,?)",
            (st.book, st.entry_id, json.dumps(st.to_json()), now_iso()),
        )

    def load_positions(self) -> list[BookState]:
        return [
            BookState.from_json(json.loads(r[0]))
            for r in self.q("select state_json from positions order by entry_id, book")
        ]

    def add_event(self, e: Event, entry_id: int) -> None:
        self.con.execute(
            "insert or ignore into events (ts, book, entry_id, kind, detail) values (?,?,?,?,?)",
            (e.ts.isoformat(), e.book, entry_id, e.kind, json.dumps(e.detail, sort_keys=True)),
        )

    def add_trade(self, st: BookState) -> None:
        assert st.exit is not None
        basis = float(st.entry_price * st.quantity)
        net = float(st.net_pnl())
        self.con.execute(
            "insert or replace into trades values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                st.book,
                st.entry_id,
                st.entry_time.isoformat(),
                float(st.entry_price),
                float(st.quantity),
                st.exit.ts.isoformat(),
                float(st.exit.price),
                st.exit_reason,
                st.exit_source,
                net,
                (net / basis * 100) if basis else 0.0,
                float(st.entry_fee + st.exit.fee),
                int((st.exit.ts - st.entry_time).total_seconds()),
                st.mfe_r(),
                st.mae_r(),
                int(st.be_activated),
            ),
        )

    def health(self, **kw: Any) -> None:
        self.con.execute(
            "insert or replace into health values (1,?,?,?,?,?,?,?,?,?)",
            (
                kw["pid"],
                kw["started_at"],
                kw.get("last_tick_at"),
                kw["ticks"],
                int(kw["ws_connected"]),
                kw["errors"],
                kw.get("last_error"),
                now_iso(),
                kw["open_positions"],
            ),
        )

    def set_observation(self, key: str, value: dict[str, Any]) -> None:
        self.con.execute(
            "insert or replace into observation values (?,?,?)",
            (key, json.dumps(value, default=str), now_iso()),
        )
