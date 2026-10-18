"""Round 3 experiment configuration (config/research/round3.yaml) — fixed, pre-registered."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.types import Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.trend_following import TrendFollowing4h, TrendFollowingBreakout

DEFAULT_CONFIG = Path(__file__).resolve().parents[4] / "config" / "research" / "round3.yaml"


def parameter_hash(params: dict[str, float]) -> str:
    """sha256[:16] of the float parameter snapshot — same formula as the trader strategy epoch."""
    snap = {k: float(v) for k, v in sorted(params.items())}
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class StrategyDef:
    strategy_id: str  # D0..D6
    hypothesis_id: str  # CONTROL | H1..H6
    strategy_version: str
    timeframe: Timeframe
    trend_timeframe: Timeframe
    horizon_days: int
    params: dict[str, float]

    @property
    def parameter_hash(self) -> str:
        return parameter_hash(self.params)

    @property
    def registry_name(self) -> str:
        return "trend_following_4h" if self.timeframe is Timeframe.H4 else "trend_following"

    @property
    def breakout_horizon(self) -> str:
        return f"{self.horizon_days}d"

    @property
    def entry_n(self) -> int:
        return int(self.params["entry_n"])

    @property
    def exit_n(self) -> int:
        return int(self.params["exit_n"])

    def build(self) -> BaseStrategy:
        """Build the strategy DIRECTLY (not via the trader/registry): D0 replica uses the same
        class the trader runs; D1..D6 use the generic breakout class on their timeframe."""
        cls = TrendFollowing4h if self.timeframe is Timeframe.H4 else TrendFollowingBreakout
        return cls(self.timeframe, self.trend_timeframe, dict(self.params))

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "hypothesis_id": self.hypothesis_id,
            "strategy_version": self.strategy_version,
            "timeframe": self.timeframe.value,
            "trend_timeframe": self.trend_timeframe.value,
            "horizon_days": self.horizon_days,
            "params": dict(self.params),
            "parameter_hash": self.parameter_hash,
        }


@dataclass(frozen=True, slots=True)
class HistoricalSpec:
    start: datetime
    end: datetime
    train_days: int
    test_days: int
    roll_days: int
    warmup_days: int
    cost_multipliers: tuple[float, ...]
    recent_2y_start: datetime
    ytd_start: datetime
    fake_breakout_max_hours: int


@dataclass(frozen=True, slots=True)
class GateSpec:
    min_oos_return_pct: float
    min_oos_profit_factor: float
    max_oos_mdd_pct: float
    min_profitable_window_ratio: float
    min_oos_trades: int
    review_min_trades: int
    cost_stress_positive: tuple[float, ...]
    recent_2y_min_return_pct: float


@dataclass(frozen=True, slots=True)
class Round3Config:
    experiment_id: str
    title: str
    control: str
    market: str
    started_at: datetime
    final_report_after: datetime
    market_db: Path
    research_db: Path
    log_file: Path
    poll_seconds: int
    warmup_days: int
    initial_virtual_equity: int
    strategies: list[StrategyDef]
    historical: HistoricalSpec
    gate: GateSpec
    raw: dict[str, Any] = field(default_factory=dict)

    def strategy(self, sid: str) -> StrategyDef:
        return next(s for s in self.strategies if s.strategy_id == sid)

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.raw, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


def load_config(path: Path | str = DEFAULT_CONFIG) -> Round3Config:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    base = p.resolve().parents[2] / "backend"  # relative DB/log paths are relative to backend/
    common = {k: float(v) for k, v in raw["common_params"].items()}
    trend_tf = Timeframe(raw.get("trend_timeframe", "1d"))
    strategies: list[StrategyDef] = []
    for sid, s in raw["strategies"].items():
        params = {
            **common,
            "entry_n": float(s["entry_n"]),
            "exit_n": float(s["exit_n"]),
            "atr_period": float(s["atr_period"]),
        }
        strategies.append(
            StrategyDef(
                strategy_id=sid,
                hypothesis_id=str(s["hypothesis"]),
                strategy_version=str(s["strategy_version"]),
                timeframe=Timeframe(str(s["timeframe"])),
                trend_timeframe=trend_tf,
                horizon_days=int(s["horizon_days"]),
                params=params,
            )
        )
    h = raw["historical"]
    g = raw["gate"]
    return Round3Config(
        experiment_id=str(raw["experiment_id"]),
        title=str(raw["title"]),
        control=str(raw["control"]),
        market=str(raw["market"]),
        started_at=_dt(raw["started_at"]),
        final_report_after=_dt(raw["final_report_after"]),
        market_db=(base / raw["market_db"]).resolve(),
        research_db=(base / raw["research_db"]).resolve(),
        log_file=(base / raw["log_file"]).resolve(),
        poll_seconds=int(raw["poll_seconds"]),
        warmup_days=int(raw["warmup_days"]),
        initial_virtual_equity=int(raw["initial_virtual_equity"]),
        strategies=strategies,
        historical=HistoricalSpec(
            start=_dt(h["start"]),
            end=_dt(h["end"]),
            train_days=int(h["train_days"]),
            test_days=int(h["test_days"]),
            roll_days=int(h["roll_days"]),
            warmup_days=int(h["warmup_days"]),
            cost_multipliers=tuple(float(x) for x in h["cost_multipliers"]),
            recent_2y_start=_dt(h["recent_2y_start"]),
            ytd_start=_dt(h["ytd_start"]),
            fake_breakout_max_hours=int(h["fake_breakout_max_hours"]),
        ),
        gate=GateSpec(
            min_oos_return_pct=float(g["min_oos_return_pct"]),
            min_oos_profit_factor=float(g["min_oos_profit_factor"]),
            max_oos_mdd_pct=float(g["max_oos_mdd_pct"]),
            min_profitable_window_ratio=float(g["min_profitable_window_ratio"]),
            min_oos_trades=int(g["min_oos_trades"]),
            review_min_trades=int(g["review_min_trades"]),
            cost_stress_positive=tuple(float(x) for x in g["cost_stress_positive"]),
            recent_2y_min_return_pct=float(g["recent_2y_min_return_pct"]),
        ),
        raw=raw,
    )
