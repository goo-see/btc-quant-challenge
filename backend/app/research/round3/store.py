"""Round 3 research DB (data/research_round3.db) — the ONLY file the shadow runner writes.

Idempotency: decisions are unique on (strategy_id, market_bar_time); fills on (strategy_id,
client_id); trades on (strategy_id, entry_time); forward tracking on (strategy_id,
signal_bar_time). All inserts are INSERT OR IGNORE so a deterministic replay after a restart
never duplicates rows; a replay that would produce a DIFFERENT decision for an existing key is
logged as a divergence (first write wins).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.backtest.metrics import TradeRecord
from app.core.types import Action
from app.research.round3.config import Round3Config, StrategyDef
from app.research.round3.shadow_engine import DecisionEvent, FillEvent

SCHEMA = """
create table if not exists experiment (
  experiment_id text primary key, title text, started_at text, created_at text,
  config_hash text, config_json text, git_head text, code_hashes_json text, market_db text);
create table if not exists strategies (
  strategy_id text primary key, experiment_id text, hypothesis_id text, strategy_version text,
  timeframe text, horizon_days integer, params_json text, parameter_hash text, initial_equity text);
create table if not exists decisions (
  id integer primary key autoincrement, experiment_id text, strategy_id text, hypothesis_id text,
  strategy_version text, timeframe text, breakout_horizon text, evaluated_at text,
  market_bar_time text, bar_open_time text, daily_close real, ema100 real, trend_ok integer,
  timeframe_close real, donchian_upper real, distance_to_breakout_pct real, breakout integer,
  donchian_lower real, atr real, position text, entry_price text, initial_stop text,
  trailing_stop text, decision text, reason text, virtual_execution text, virtual_fill_price text,
  virtual_quantity text, virtual_fill_time text, realized_pnl text, equity text, replay integer,
  unique(strategy_id, market_bar_time));
create table if not exists fills (
  id integer primary key autoincrement, strategy_id text, client_id text, side text, kind text,
  price text, volume text, funds text, fee text, ts text, bar_open_time text, reason text,
  signal_bar_time text, unique(strategy_id, client_id));
create table if not exists trades (
  id integer primary key autoincrement, strategy_id text, entry_time text, exit_time text,
  entry_price text, exit_price text, quantity text, net_pnl text, fees text, return_pct text,
  holding_minutes integer, entry_reason text, exit_reason text, unique(strategy_id, entry_time));
create table if not exists rejects (
  id integer primary key autoincrement, strategy_id text, signal_bar_time text, reason text,
  unique(strategy_id, signal_bar_time));
create table if not exists portfolio (
  strategy_id text primary key, updated_at text, last_bar_time text, cash text, btc text,
  entry_price text, entry_time text, initial_stop text, trailing_stop text,
  highest_since_entry text, realized_pnl text, unrealized_pnl text, equity text, hwm text,
  max_drawdown_pct text, fees text, trade_count integer, evaluations integer, buy_signals integer,
  sell_signals integer, pending text);
create table if not exists forward_tracking (
  id integer primary key autoincrement, strategy_id text, signal_bar_time text, signal_price text,
  fill_time text, fill_price text, quantity text, initial_stop text,
  d0_decision_same_bar text, d0_entry_time text, lead_hours_vs_d0 real,
  ret_4h_pct real, ret_8h_pct real, ret_12h_pct real, ret_24h_pct real,
  mfe_pct real, mae_pct real, stop_hit integer, exit_time text, exit_price text, exit_reason text,
  pnl text, status text, updated_at text, unique(strategy_id, signal_bar_time));
create table if not exists d0_consistency (
  market_bar_time text primary key, checked_at text, replica_decision text, replica_reason text,
  replica_upper real, replica_lower real, replica_ema real, replica_atr real, real_decision text,
  real_reason text, real_upper real, real_lower real, real_ema real, real_atr real,
  decision_match integer, channel_match integer, ema_diff_pct real);
create table if not exists divergences (
  id integer primary key autoincrement, ts text, strategy_id text, market_bar_time text,
  stored_decision text, replay_decision text, detail text);
create table if not exists heartbeat (
  id integer primary key check (id = 1), pid integer, started_at text, last_tick text,
  last_1m_open text, ticks integer, errors integer, last_error text, replay_done integer);
create table if not exists runner_log (
  id integer primary key autoincrement, ts text, level text, type text, message text);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


class ResearchStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(str(path), timeout=30)
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ---------------------------------------------------------------- experiment
    def ensure_experiment(
        self, cfg: Round3Config, *, git_head: str, code_hashes: dict[str, str]
    ) -> tuple[bool, str]:
        """Create the experiment row on first run. Returns (created, stored_config_hash).
        An existing experiment is NEVER reset; a config hash mismatch is reported by the caller."""
        row = self.con.execute(
            "select config_hash from experiment where experiment_id=?", (cfg.experiment_id,)
        ).fetchone()
        if row:
            return False, str(row[0])
        self.con.execute(
            "insert into experiment values (?,?,?,?,?,?,?,?,?)",
            (
                cfg.experiment_id,
                cfg.title,
                cfg.started_at.isoformat(),
                now_iso(),
                cfg.config_hash,
                json.dumps(cfg.raw, default=str),
                git_head,
                json.dumps(code_hashes),
                str(cfg.market_db),
            ),
        )
        for s in cfg.strategies:
            self.con.execute(
                "insert or ignore into strategies values (?,?,?,?,?,?,?,?,?)",
                (
                    s.strategy_id,
                    cfg.experiment_id,
                    s.hypothesis_id,
                    s.strategy_version,
                    s.timeframe.value,
                    s.horizon_days,
                    json.dumps(s.params, sort_keys=True),
                    s.parameter_hash,
                    str(cfg.initial_virtual_equity),
                ),
            )
        self.con.commit()
        return True, cfg.config_hash

    def log(self, level: str, typ: str, message: str) -> None:
        self.con.execute(
            "insert into runner_log (ts, level, type, message) values (?,?,?,?)",
            (now_iso(), level, typ, message[:2000]),
        )

    # ---------------------------------------------------------------- decisions
    def add_decision(
        self, cfg: Round3Config, sd: StrategyDef, ev: DecisionEvent, *, replay: bool
    ) -> bool:
        sig, c = ev.signal, ev.bar
        ind = sig.indicators
        upper = ind.get("channel_upper")
        close = float(c.close)
        row = (
            cfg.experiment_id,
            sd.strategy_id,
            sd.hypothesis_id,
            sd.strategy_version,
            sd.timeframe.value,
            sd.breakout_horizon,
            now_iso(),
            c.close_time.isoformat(),
            c.open_time.isoformat(),
            ind.get("close_trend"),
            ind.get("trend_ema"),
            (
                None
                if "trend_ema" not in ind
                else int(float(ind.get("close_trend", 0)) > float(ind["trend_ema"]))
            ),
            close,
            upper,
            ((upper - close) / close * 100) if upper is not None else None,
            None if upper is None else int(close > upper),
            ind.get("channel_lower"),
            ind.get("atr"),
            _s(ev.position_qty),
            _s(ev.entry_price),
            _s(ev.initial_stop),
            _s(ev.new_stop),
            f"{sig.action.value}_SHADOW" if sig.action is not Action.HOLD else "HOLD",
            sig.reason,
            "PENDING_NEXT_BAR" if sig.action is not Action.HOLD else "NONE",
            None,
            None,
            None,
            str(ev.realized_pnl),
            str(ev.equity),
            int(replay),
        )
        cur = self.con.execute(
            "insert or ignore into decisions (experiment_id, strategy_id, hypothesis_id, strategy_version, timeframe, breakout_horizon, evaluated_at, market_bar_time, bar_open_time, daily_close, ema100, trend_ok, timeframe_close, donchian_upper, distance_to_breakout_pct, breakout, donchian_lower, atr, position, entry_price, initial_stop, trailing_stop, decision, reason, virtual_execution, virtual_fill_price, virtual_quantity, virtual_fill_time, realized_pnl, equity, replay) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        if cur.rowcount == 0:
            stored = self.con.execute(
                "select decision, reason, timeframe_close from decisions where strategy_id=? and market_bar_time=?",
                (sd.strategy_id, c.close_time.isoformat()),
            ).fetchone()
            if stored and (stored[0] != row[22] or abs(float(stored[2]) - close) > 1e-6):
                self.con.execute(
                    "insert into divergences (ts, strategy_id, market_bar_time, stored_decision, replay_decision, detail) values (?,?,?,?,?,?)",
                    (
                        now_iso(),
                        sd.strategy_id,
                        c.close_time.isoformat(),
                        f"{stored[0]} {stored[1]} close={stored[2]}",
                        f"{row[22]} {row[23]} close={close}",
                        "replay produced a different decision for an existing key (first write wins)",
                    ),
                )
            return False
        return True

    def add_fill(self, sd: StrategyDef, ev: FillEvent) -> bool:
        f = ev.fill
        cur = self.con.execute(
            "insert or ignore into fills (strategy_id, client_id, side, kind, price, volume, funds, fee, ts, bar_open_time, reason, signal_bar_time) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sd.strategy_id,
                f.order_client_id,
                ev.side,
                ev.kind,
                str(f.price),
                str(f.volume),
                str(f.funds),
                str(f.fee),
                f.timestamp.isoformat(),
                ev.bar_open_time.isoformat(),
                ev.reason,
                ev.signal_bar_close.isoformat() if ev.signal_bar_close else None,
            ),
        )
        if ev.signal_bar_close is not None:
            self.con.execute(
                "update decisions set virtual_execution=?, virtual_fill_price=?, virtual_quantity=?, virtual_fill_time=? where strategy_id=? and market_bar_time=? and virtual_execution='PENDING_NEXT_BAR'",
                (
                    f"{ev.side}_FILLED",
                    str(f.price),
                    str(f.volume),
                    f.timestamp.isoformat(),
                    sd.strategy_id,
                    ev.signal_bar_close.isoformat(),
                ),
            )
        if ev.side == "BUY":
            self.con.execute(
                "insert or ignore into forward_tracking (strategy_id, signal_bar_time, signal_price, fill_time, fill_price, quantity, initial_stop, stop_hit, status, updated_at) values (?,?,?,?,?,?,?,0,'OPEN',?)",
                (
                    sd.strategy_id,
                    ev.signal_bar_close.isoformat()
                    if ev.signal_bar_close
                    else f.timestamp.isoformat(),
                    None,
                    f.timestamp.isoformat(),
                    str(f.price),
                    str(f.volume),
                    None,
                    now_iso(),
                ),
            )
        return cur.rowcount > 0

    def add_reject(self, sd: StrategyDef, signal_bar_close: datetime, reason: str) -> None:
        self.con.execute(
            "insert or ignore into rejects (strategy_id, signal_bar_time, reason) values (?,?,?)",
            (sd.strategy_id, signal_bar_close.isoformat(), reason),
        )
        self.con.execute(
            "update decisions set virtual_execution=? where strategy_id=? and market_bar_time=? and virtual_execution='PENDING_NEXT_BAR'",
            (f"REJECTED:{reason.split(' ')[0]}", sd.strategy_id, signal_bar_close.isoformat()),
        )

    def add_trade(self, sd: StrategyDef, t: TradeRecord) -> bool:
        cur = self.con.execute(
            "insert or ignore into trades (strategy_id, entry_time, exit_time, entry_price, exit_price, quantity, net_pnl, fees, return_pct, holding_minutes, entry_reason, exit_reason) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sd.strategy_id,
                t.entry_time,
                t.exit_time,
                str(t.entry_price),
                str(t.exit_price),
                str(t.quantity),
                str(t.net_pnl),
                str(t.fees),
                str(t.return_pct),
                t.holding_minutes,
                t.entry_reason,
                t.exit_reason,
            ),
        )
        # close the forward-tracking row opened by this trade's BUY fill
        self.con.execute(
            "update forward_tracking set status='CLOSED', exit_time=?, exit_price=?, exit_reason=?, pnl=?, stop_hit=?, updated_at=? where strategy_id=? and fill_time=? and status='OPEN'",
            (
                t.exit_time,
                str(t.exit_price),
                t.exit_reason,
                str(t.net_pnl),
                int(t.exit_reason.startswith("STOP")),
                now_iso(),
                sd.strategy_id,
                t.entry_time,
            ),
        )
        return cur.rowcount > 0

    def save_portfolio(self, sd: StrategyDef, snap: dict[str, Any]) -> None:
        self.con.execute(
            "insert or replace into portfolio values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sd.strategy_id,
                now_iso(),
                snap["last_bar_time"],
                snap["cash"],
                snap["btc"],
                snap["entry_price"],
                snap["entry_time"],
                snap["initial_stop"],
                snap["trailing_stop"],
                snap["highest_since_entry"],
                snap["realized_pnl"],
                snap["unrealized_pnl"],
                snap["equity"],
                snap["hwm"],
                snap["max_drawdown_pct"],
                snap["fees"],
                snap["trade_count"],
                snap["evaluations"],
                snap["buy_signals"],
                snap["sell_signals"],
                snap["pending"],
            ),
        )

    def heartbeat(
        self,
        *,
        pid: int,
        started_at: str,
        last_1m_open: str | None,
        ticks: int,
        errors: int,
        last_error: str | None,
        replay_done: bool,
    ) -> None:
        self.con.execute(
            "insert or replace into heartbeat values (1,?,?,?,?,?,?,?,?)",
            (pid, started_at, now_iso(), last_1m_open, ticks, errors, last_error, int(replay_done)),
        )

    def commit(self) -> None:
        self.con.commit()

    # ---------------------------------------------------------------- queries
    def q(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, args).fetchall()

    def open_tracking(self) -> list[tuple[Any, ...]]:
        return self.q(
            "select id, strategy_id, signal_bar_time, fill_time, fill_price, status, exit_time, ret_4h_pct, ret_8h_pct, ret_12h_pct, ret_24h_pct, d0_entry_time from forward_tracking where status='OPEN' or ret_24h_pct is null or d0_entry_time is null or lead_hours_vs_d0 is null"
        )
