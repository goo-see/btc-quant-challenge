"""Round 3 shadow runner — a pure local process (launchd `com.quant.shadow-round3`).

    market DB (read-only)  ->  1h history warm-up  ->  live 1m stream  ->  7 ShadowEngines
                                                                            (D0 replica + D1..D6)
                                                       -> data/research_round3.db (only write target)

- NO network, NO Claude/LLM, NO exchange gateway, NO OrderManager, NO RiskManager, NO quant.db writes.
- Deterministic: on every start the engines are rebuilt by replaying the same candles from
  `started_at - warmup_days`; decisions/fills/trades are INSERT OR IGNORE keyed by strategy + bar.
- Stage 1 base = 1h (history table) up to the UTC-midnight boundary before `started_at`;
  stage 2 base = 1m (canonical WS stream) from that boundary onward (the same staged warm-up the
  trader uses, but the boundary buckets are flushed instead of dropped).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.backtest.engine import BacktestConfig
from app.core.types import Timeframe
from app.execution.fill_model import FillParams
from app.research.round3.config import DEFAULT_CONFIG, Round3Config, StrategyDef, load_config
from app.research.round3.market_reader import MarketReader
from app.research.round3.shadow_engine import BarEvents, ShadowEngine
from app.research.round3.store import ResearchStore, now_iso
from app.risk.position_sizer import SizingParams

# Identical to config/config.yaml risk (BACKTEST/PAPER/LIVE share the same sizing and costs);
# duplicated here on purpose so this process never imports the trader settings/env (.env secrets).
FEE_RATE = Decimal("0.0005")
SLIPPAGE_BPS = Decimal(3)
MAX_POSITION_PCT = Decimal("0.30")
MAX_ORDER_KRW = Decimal(30000)
MIN_ORDER_KRW = Decimal(5000)
MINIMUM_KRW_BALANCE = Decimal(1000)
CLOSED_1M_LAG = timedelta(minutes=2)  # a 1m row is final once the next minute has started


def backtest_config(initial_equity: int) -> BacktestConfig:
    return BacktestConfig(
        initial_equity=Decimal(initial_equity),
        fill=FillParams(fee_rate=FEE_RATE, slippage_bps=SLIPPAGE_BPS),
        sizing=SizingParams(
            max_position_pct=MAX_POSITION_PCT,
            max_order_krw=MAX_ORDER_KRW,
            min_order_krw=MIN_ORDER_KRW,
            fee_rate=FEE_RATE,
            minimum_krw_balance=MINIMUM_KRW_BALANCE,
        ),
    )


def code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    files = [
        "app/strategy/trend_following.py",
        "app/backtest/engine.py",
        "app/execution/fill_model.py",
        "app/risk/position_sizer.py",
        "app/portfolio/accounting.py",
        "app/market/candle_engine.py",
        "app/market/indicators.py",
        "app/research/round3/shadow_engine.py",
        "app/research/round3/runner.py",
    ]
    return {f: hashlib.sha256((root / f).read_bytes()).hexdigest()[:16] for f in files}


def git_head() -> str:
    """HEAD sha read from .git directly — no subprocess, no PATH lookup."""
    try:
        root = Path(__file__).resolve().parents[4]
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            return (root / ".git" / head[5:]).read_text().strip()
        return head
    except OSError:
        return "unknown"


def next_close(tf: Timeframe, now: datetime) -> datetime:
    secs = int(now.timestamp())
    size = tf.minutes * 60
    return datetime.fromtimestamp(secs - secs % size + size, tz=UTC)


class ShadowRunner:
    def __init__(self, cfg: Round3Config) -> None:
        self.cfg = cfg
        self.cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.market = MarketReader(cfg.market_db, cfg.market)
        self.store = ResearchStore(cfg.research_db)
        self.engines: dict[str, ShadowEngine] = {}
        self.boundary = cfg.started_at.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.last_1m: datetime | None = None
        self.ticks = 0
        self.errors = 0
        self.last_error: str | None = None
        self.started_iso = now_iso()
        self.replay_done = False

    # ------------------------------------------------------------------ logging
    def log(self, msg: str, level: str = "INFO", typ: str = "runner") -> None:
        line = f"{now_iso()} [{level}] {typ}: {msg}"
        with self.cfg.log_file.open("a") as f:
            f.write(line + "\n")
        self.store.log(level, typ, msg)

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        created, stored_hash = self.store.ensure_experiment(
            self.cfg, git_head=git_head(), code_hashes=code_hashes()
        )
        if created:
            self.log(f"experiment {self.cfg.experiment_id} created (config {self.cfg.config_hash})")
        elif stored_hash != self.cfg.config_hash:
            self.log(
                f"config hash changed since experiment creation ({stored_hash} -> {self.cfg.config_hash}); "
                "experiment is NOT reset — running with the file config",
                "WARNING",
            )
        bt_cfg = backtest_config(self.cfg.initial_virtual_equity)
        for sd in self.cfg.strategies:
            self.engines[sd.strategy_id] = ShadowEngine(
                sd.build(),
                bt_cfg,
                start_at=self.cfg.started_at,
                base=Timeframe.H1,
                strategy_id=sd.strategy_id,
            )
        self.store.commit()
        self._replay()

    def _replay(self) -> None:
        t0 = time.monotonic()
        h1 = self.market.load(
            Timeframe.H1, self.boundary - timedelta(days=self.cfg.warmup_days), self.boundary
        )
        for c in h1:
            for eng in self.engines.values():
                eng.on_bar(c)  # all before started_at -> warm-up only, no events
        for eng in self.engines.values():
            eng.switch_base(Timeframe.M1, self.boundary)
        m1 = self.market.load(Timeframe.M1, self.boundary, self._closed_1m_cutoff())
        n_dec = n_fill = 0
        for c in m1:
            d, f = self._feed(c, replay=True)
            n_dec += d
            n_fill += f
        self.last_1m = m1[-1].open_time if m1 else None
        for sd in self.cfg.strategies:
            self.store.save_portfolio(sd, self.engines[sd.strategy_id].snapshot())
        self.replay_done = True
        self.store.commit()
        self.log(
            f"replay done: 1h bars {len(h1)} ({h1[0].open_time.isoformat() if h1 else '-'} .. {self.boundary.isoformat()}), "
            f"1m bars {len(m1)} (.. {self.last_1m.isoformat() if self.last_1m else '-'}), "
            f"new decisions {n_dec}, new fills {n_fill}, {time.monotonic() - t0:.1f}s"
        )

    def _closed_1m_cutoff(self) -> datetime:
        return (
            datetime.now(UTC).replace(second=0, microsecond=0)
            - CLOSED_1M_LAG
            + timedelta(minutes=1)
        )

    # ------------------------------------------------------------------ feeding
    def _feed(self, bar: Any, *, replay: bool) -> tuple[int, int]:
        n_dec = n_fill = 0
        for sd in self.cfg.strategies:
            ev: BarEvents = self.engines[sd.strategy_id].on_bar(bar)
            for r in ev.rejects:
                self.store.add_reject(sd, r.signal_bar_close, r.reason)
            for f in ev.fills:
                if self.store.add_fill(sd, f):
                    n_fill += 1
                    if not replay:
                        self.log(
                            f"{sd.strategy_id} {f.side} {f.kind} {f.fill.volume} BTC @ {f.fill.price:.0f} ({f.reason})",
                            typ="fill",
                        )
            for t in ev.trades:
                self.store.add_trade(sd, t)
                if not replay:
                    self.log(
                        f"{sd.strategy_id} TRADE closed net {t.net_pnl} KRW ({t.exit_reason}) held {t.holding_minutes}m",
                        typ="trade",
                    )
            for d in ev.decisions:
                if self.store.add_decision(self.cfg, sd, d, replay=replay):
                    n_dec += 1
                    if not replay:
                        ind = d.signal.indicators
                        self.log(
                            f"{sd.strategy_id} {d.signal.action.value} {d.signal.reason} bar={d.bar.close_time.isoformat()} close={float(d.bar.close):.0f} upper={ind.get('channel_upper', 0):.0f} lower={ind.get('channel_lower', 0):.0f} eq={d.equity}",
                            typ="decision",
                        )
        return n_dec, n_fill

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        self.ticks += 1
        m1 = self.market.load(Timeframe.M1, None, self._closed_1m_cutoff(), after=self.last_1m)
        changed = False
        for c in m1:
            if self.last_1m is not None and c.open_time <= self.last_1m:
                continue
            self._feed(c, replay=False)
            self.last_1m = c.open_time
            changed = True
        if changed:
            for sd in self.cfg.strategies:
                self.store.save_portfolio(sd, self.engines[sd.strategy_id].snapshot())
        self._update_forward_tracking()
        self._check_d0_consistency()
        self.store.heartbeat(
            pid=os.getpid(),
            started_at=self.started_iso,
            last_1m_open=self.last_1m.isoformat() if self.last_1m else None,
            ticks=self.ticks,
            errors=self.errors,
            last_error=self.last_error,
            replay_done=self.replay_done,
        )
        self.store.commit()

    # ------------------------------------------------------------------ forward tracking
    def _update_forward_tracking(self) -> None:
        rows = self.store.open_tracking()
        if not rows:
            return
        now = datetime.now(UTC)
        d0_buys = [
            datetime.fromisoformat(str(r[0]))
            for r in self.store.q(
                "select ts from fills where strategy_id=? and side='BUY' order by ts",
                (self.cfg.control,),
            )
        ]
        for (
            rid,
            sid,
            sig_bar,
            fill_time,
            fill_price,
            status,
            exit_time,
            r4,
            r8,
            r12,
            r24,
            _d0,
        ) in rows:
            ft = datetime.fromisoformat(str(fill_time))
            fp = Decimal(str(fill_price))
            end = (
                datetime.fromisoformat(str(exit_time)) if status == "CLOSED" and exit_time else now
            )
            hi, lo, _ = self.market.range_stats(Timeframe.M1, ft, end)
            mfe = float((hi - fp) / fp * 100) if hi is not None else None
            mae = float((lo - fp) / fp * 100) if lo is not None else None
            rets: dict[str, float | None] = {}
            for h, cur in (
                ("ret_4h_pct", r4),
                ("ret_8h_pct", r8),
                ("ret_12h_pct", r12),
                ("ret_24h_pct", r24),
            ):
                if cur is not None:
                    rets[h] = float(cur)
                    continue
                at = ft + timedelta(hours=int(h.split("_")[1][:-1])) - timedelta(minutes=1)
                px = (
                    self.market.close_at(Timeframe.M1, at)
                    if at + timedelta(minutes=1) <= now - CLOSED_1M_LAG
                    else None
                )
                rets[h] = float((px - fp) / fp * 100) if px is not None else None
            sig_dt = datetime.fromisoformat(str(sig_bar))
            sig_px = self.market.close_at(Timeframe.M1, sig_dt - timedelta(minutes=1))
            d0_same = self.store.q(
                "select decision, reason from decisions where strategy_id=? and market_bar_time>=? order by market_bar_time limit 1",
                (self.cfg.control, sig_dt.isoformat()),
            )
            d0_entry, lead = None, None
            if sid != self.cfg.control:
                near = [b for b in d0_buys if abs((b - ft).total_seconds()) <= 10 * 86400]
                if near:
                    d0_entry = min(near, key=lambda b: abs((b - ft).total_seconds()))
                    lead = (
                        d0_entry - ft
                    ).total_seconds() / 3600  # >0 -> this strategy entered earlier
            self.store.con.execute(
                "update forward_tracking set signal_price=?, d0_decision_same_bar=?, d0_entry_time=?, lead_hours_vs_d0=?, ret_4h_pct=?, ret_8h_pct=?, ret_12h_pct=?, ret_24h_pct=?, mfe_pct=?, mae_pct=?, updated_at=? where id=?",
                (
                    str(sig_px) if sig_px is not None else None,
                    f"{d0_same[0][0]} {d0_same[0][1]}" if d0_same else None,
                    d0_entry.isoformat() if d0_entry else None,
                    lead,
                    rets["ret_4h_pct"],
                    rets["ret_8h_pct"],
                    rets["ret_12h_pct"],
                    rets["ret_24h_pct"],
                    mfe,
                    mae,
                    now_iso(),
                    rid,
                ),
            )

    # ------------------------------------------------------------------ D0 consistency
    def _check_d0_consistency(self) -> None:
        """Compare the D0 replica's latest 4h decision with the real D0 decision snapshot
        (bot_state.decision_PAPER, read-only). Evidence that the shadow replicates D0 and that D0
        itself is unaffected."""
        raw = self.market.bot_state("decision_PAPER")
        if not raw:
            return
        real = json.loads(raw)
        bar = real.get("market_bar_time")
        if not bar:
            return
        rep = self.store.q(
            "select decision, reason, donchian_upper, donchian_lower, ema100, atr from decisions where strategy_id=? and market_bar_time=?",
            (self.cfg.control, datetime.fromisoformat(bar).astimezone(UTC).isoformat()),
        )
        if not rep:
            return
        r = rep[0]
        rdec = "HOLD" if r[0] == "HOLD" else str(r[0]).replace("_SHADOW", "")
        real_ema = float(real.get("daily_ema") or 0)
        ema_diff = (float(r[4] or 0) - real_ema) / real_ema * 100 if real_ema else None
        self.store.con.execute(
            "insert or replace into d0_consistency values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.fromisoformat(bar).astimezone(UTC).isoformat(),
                now_iso(),
                rdec,
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                real.get("decision"),
                real.get("reason"),
                real.get("donchian_upper"),
                real.get("donchian_lower"),
                real_ema,
                real.get("atr"),
                int(rdec == real.get("decision")),
                int(r[2] == real.get("donchian_upper") and r[3] == real.get("donchian_lower")),
                ema_diff,
            ),
        )

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        self.log(
            f"shadow runner start pid={os.getpid()} python={sys.version.split()[0]} (loopback-free, no LLM, market DB read-only)"
        )
        self.bootstrap()
        while True:
            try:
                self.tick()
            except Exception as exc:  # never die on a transient error
                self.errors += 1
                self.last_error = f"{now_iso()} {exc!r}"
                self.log(f"tick error: {exc!r}\n{traceback.format_exc()[-1500:]}", "ERROR")
                try:
                    self.store.commit()
                except Exception as exc2:
                    self.last_error = f"{self.last_error} / commit: {exc2!r}"
            time.sleep(self.cfg.poll_seconds)

    def next_evaluations(self, now: datetime | None = None) -> dict[str, str]:
        now = now or datetime.now(UTC)
        return {
            sd.strategy_id: next_close(sd.timeframe, now).isoformat() for sd in self.cfg.strategies
        }


def strategy_defs(cfg: Round3Config) -> list[StrategyDef]:
    return list(cfg.strategies)


def main(argv: list[str] | None = None) -> int:
    path = Path(argv[1]) if argv and len(argv) > 1 else DEFAULT_CONFIG
    runner = ShadowRunner(load_config(path))
    runner.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
