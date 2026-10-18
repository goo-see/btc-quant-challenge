"""Fair strategy comparison harness (DAY 5).

Identical for every strategy: dataset (same 1m -> 15m base), walk-forward windows, fee,
slippage, sizing, FillModel (STOP-first), metrics. Coarse grid on train -> evaluate OOS ->
parameter-neighborhood stability -> cost stress (1.0x / 1.5x / 2.0x) -> approval matrix.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from app.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from app.backtest.walk_forward import WalkForwardResult, run_walk_forward
from app.core.types import Candle
from app.execution.fill_model import FillParams
from app.strategy.registry import build_strategy


@dataclass(frozen=True, slots=True)
class StrategySpec:
    key: str  # "A" | "B" | "C"
    name: str  # registry name
    timeframe: str
    trend_timeframe: str | None
    base_params: dict[str, float]
    grid: dict[str, list[float]]
    description: str


@dataclass(slots=True)
class ApprovalCriteria:
    min_oos_return_pct: float = 0.0
    min_oos_profit_factor: float = 1.20
    max_oos_mdd_pct: float = 10.0
    min_profitable_window_ratio: float = 0.60
    min_oos_trades: int = 50
    min_neighborhood_positive_ratio: float = 0.60  # share of neighbours with positive expectancy
    max_cost_stress_collapse: float = 0.5  # 1.5x costs must keep >= 50% of 1.0x OOS return (if >0)


@dataclass(slots=True)
class OosSummary:
    net_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy: float
    fees: float
    exposure_pct: float
    profitable_windows: int
    windows: int
    buy_hold_return_pct: float
    buy_hold_mdd_pct: float


@dataclass(slots=True)
class StrategyReport:
    spec: StrategySpec
    full_backtest: dict[str, Any]
    walk_forward: dict[str, Any]
    oos: OosSummary
    stability: dict[str, Any]
    cost_stress: dict[str, dict[str, Any]]
    matrix: dict[str, bool]
    approved: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def buy_hold_mdd_pct(candles: list[Candle]) -> float:
    hwm = float(candles[0].close)
    mdd = 0.0
    for c in candles:
        px = float(c.close)
        hwm = max(hwm, px)
        mdd = max(mdd, (hwm - px) / hwm)
    return mdd * 100


def oos_summary(wf: WalkForwardResult, candles: list[Candle]) -> OosSummary:
    """Aggregate OOS test windows. PF/expectancy from summed window gross win/loss."""
    trades = wins = losses = 0
    gross_win = gross_loss = fees = 0.0
    exposure = 0.0
    for w in wf.windows:
        m = w.test_metrics
        tc = int(m["trade_count"])
        trades += tc
        wins += int(m["win_count"])
        losses += int(m["loss_count"])
        gross_win += float(m["average_win"]) * int(m["win_count"])
        gross_loss += float(m["average_loss"]) * int(m["loss_count"])
        fees += float(m["fees_paid"])
        exposure += float(m["exposure_pct"])
    n = len(wf.windows) or 1
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    return OosSummary(
        net_return_pct=float(wf.stitched_test_return_pct),
        profit_factor=pf,
        max_drawdown_pct=float(wf.stitched_test_mdd_pct),
        trades=trades,
        wins=wins,
        losses=losses,
        win_rate=(wins / trades) if trades else 0.0,
        expectancy=((gross_win - gross_loss) / trades) if trades else 0.0,
        fees=fees,
        exposure_pct=exposure / n,
        profitable_windows=wf.oos_profitable_windows,
        windows=len(wf.windows),
        buy_hold_return_pct=(float(candles[-1].close) / float(candles[0].close) - 1) * 100,
        buy_hold_mdd_pct=buy_hold_mdd_pct(candles),
    )


def neighborhood(params: dict[str, float], grid: dict[str, list[float]]) -> list[dict[str, float]]:
    """All grid points that differ from `params` in exactly one dimension by one step."""
    out: list[dict[str, float]] = []
    for k, values in grid.items():
        if k not in params or params[k] not in values:
            continue
        i = values.index(params[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                out.append({**params, k: values[j]})
    return out


def stability_check(
    spec: StrategySpec, candles: list[Candle], wf: WalkForwardResult, cfg: BacktestConfig
) -> dict[str, Any]:
    """For each WF window, evaluate the chosen params' one-step neighbours on the TRAIN slice.

    cliff = chosen train return > 0 while the median neighbour return <= 0 (isolated optimum).
    """
    windows_out: list[dict[str, Any]] = []
    positive = total = cliffs = 0
    for w in wf.windows:
        train = [c for c in candles if w.train_start <= c.open_time < w.train_end]
        chosen = {**spec.base_params, **w.best_params}
        neigh = neighborhood(chosen, spec.grid)
        rets: list[float] = []
        for p in neigh:
            r = BacktestEngine(
                build_strategy(spec.name, spec.timeframe, spec.trend_timeframe, p), cfg
            ).run(train)
            rets.append(float(r.metrics.net_return_pct))
            total += 1
            positive += 1 if r.metrics.expectancy > 0 else 0
        med = sorted(rets)[len(rets) // 2] if rets else 0.0
        chosen_ret = float(w.train_metrics["net_return_pct"])
        cliff = chosen_ret > 0 and med <= 0
        cliffs += 1 if cliff else 0
        windows_out.append(
            {
                "train_end": w.train_end.isoformat(),
                "chosen": w.best_params,
                "chosen_train_return_pct": chosen_ret,
                "neighbour_median_return_pct": med,
                "neighbours": len(neigh),
                "cliff": cliff,
            }
        )
    return {
        "neighbour_positive_ratio": (positive / total) if total else 0.0,
        "neighbours_evaluated": total,
        "cliff_windows": cliffs,
        "windows": windows_out,
    }


def run_full_backtest(
    spec: StrategySpec, candles: list[Candle], cfg: BacktestConfig
) -> BacktestResult:
    return BacktestEngine(
        build_strategy(spec.name, spec.timeframe, spec.trend_timeframe, spec.base_params), cfg
    ).run(candles)


def with_cost_multiplier(cfg: BacktestConfig, mult: float) -> BacktestConfig:
    fee = cfg.fill.fee_rate * Decimal(str(mult))
    slip = cfg.fill.slippage_bps * Decimal(str(mult))
    return BacktestConfig(
        cfg.initial_equity, FillParams(fee_rate=fee, slippage_bps=slip), cfg.sizing
    )


def evaluate_strategy(
    spec: StrategySpec,
    candles: list[Candle],
    cfg: BacktestConfig,
    *,
    train_days: int,
    test_days: int,
    criteria: ApprovalCriteria,
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
) -> StrategyReport:
    full = run_full_backtest(spec, candles, cfg)
    wf = run_walk_forward(
        candles=candles,
        strategy_name=spec.name,
        timeframe=spec.timeframe,
        trend_timeframe=spec.trend_timeframe,
        base_params=spec.base_params,
        grid=spec.grid,
        cfg=cfg,
        train_days=train_days,
        test_days=test_days,
    )
    oos = oos_summary(wf, candles)
    stab = stability_check(spec, candles, wf, cfg)
    stress: dict[str, dict[str, Any]] = {}
    for m in cost_multipliers:
        if m == 1.0:
            stress["1.0x"] = {
                "oos_return_pct": oos.net_return_pct,
                "oos_pf": oos.profit_factor,
                "oos_mdd_pct": oos.max_drawdown_pct,
                "trades": oos.trades,
            }
            continue
        wfm = run_walk_forward(
            candles=candles,
            strategy_name=spec.name,
            timeframe=spec.timeframe,
            trend_timeframe=spec.trend_timeframe,
            base_params=spec.base_params,
            grid=spec.grid,
            cfg=with_cost_multiplier(cfg, m),
            train_days=train_days,
            test_days=test_days,
        )
        om = oos_summary(wfm, candles)
        stress[f"{m}x"] = {
            "oos_return_pct": om.net_return_pct,
            "oos_pf": om.profit_factor,
            "oos_mdd_pct": om.max_drawdown_pct,
            "trades": om.trades,
        }
    base_ret = oos.net_return_pct
    s15 = stress.get("1.5x", {}).get("oos_return_pct", base_ret)
    cost_ok = (base_ret <= 0) or (s15 >= base_ret * criteria.max_cost_stress_collapse)
    win_ratio = (oos.profitable_windows / oos.windows) if oos.windows else 0.0
    matrix = {
        "oos_return_gt_0": oos.net_return_pct > criteria.min_oos_return_pct,
        "oos_pf_ge_1.20": oos.profit_factor >= criteria.min_oos_profit_factor,
        "oos_mdd_le_10": oos.max_drawdown_pct <= criteria.max_oos_mdd_pct,
        "profitable_windows_ge_60pct": win_ratio >= criteria.min_profitable_window_ratio,
        "oos_trades_ge_50": oos.trades >= criteria.min_oos_trades,
        "parameter_stability": stab["neighbour_positive_ratio"]
        >= criteria.min_neighborhood_positive_ratio
        and stab["cliff_windows"] == 0,
        "cost_stress_1.5x": cost_ok,
    }
    notes: list[str] = []
    if stab["cliff_windows"]:
        notes.append(f"parameter cliff in {stab['cliff_windows']} window(s): overfit risk")
    if base_ret > 0 and not cost_ok:
        notes.append("OOS return collapses under 1.5x costs")
    grid_size = math.prod(len(v) for v in spec.grid.values())
    return StrategyReport(
        spec=spec,
        full_backtest={**full.metrics.to_dict(), "signals": full.signals},
        walk_forward={
            "grid_size": grid_size,
            "train_days": train_days,
            "test_days": test_days,
            "windows": [
                {
                    "train_start": w.train_start.isoformat(),
                    "train_end": w.train_end.isoformat(),
                    "test_end": w.test_end.isoformat(),
                    "best_params": w.best_params,
                    "train_return_pct": w.train_metrics["net_return_pct"],
                    "test_return_pct": w.test_metrics["net_return_pct"],
                    "test_mdd_pct": w.test_metrics["max_drawdown_pct"],
                    "test_trades": w.test_metrics["trade_count"],
                }
                for w in wf.windows
            ],
            "param_stability": wf.param_stability,
        },
        oos=oos,
        stability=stab,
        cost_stress=stress,
        matrix=matrix,
        approved=all(matrix.values()),
        notes=notes,
    )


def _v(x: float | Decimal | str) -> float:
    return float(x)


def format_report(reports: list[StrategyReport]) -> str:
    lines: list[str] = []
    for r in reports:
        m = r.full_backtest
        o = r.oos
        lines += [
            f"## {r.spec.key}. {r.spec.name} — {r.spec.description}",
            f"TF {r.spec.timeframe} / ref {r.spec.trend_timeframe} · base {r.spec.base_params}",
            f"grid {r.spec.grid} ({r.walk_forward['grid_size']} combos)",
            "",
            "| metric | FULL backtest (base params) | OOS (walk-forward stitched) | Buy & Hold |",
            "|---|---|---|---|",
            f"| Net return | {_v(m['net_return_pct']):+.2f}% | {o.net_return_pct:+.2f}% | {o.buy_hold_return_pct:+.2f}% |",
            f"| Max drawdown | {_v(m['max_drawdown_pct']):.2f}% | {o.max_drawdown_pct:.2f}% | {o.buy_hold_mdd_pct:.2f}% |",
            f"| Trades | {m['trade_count']} | {o.trades} | — |",
            f"| Profit factor | {_v(m['profit_factor']):.2f} | {o.profit_factor:.2f} | — |",
            f"| Win rate | {_v(m['win_rate']) * 100:.1f}% | {o.win_rate * 100:.1f}% | — |",
            f"| Expectancy | ₩{_v(m['expectancy']):,.0f} | ₩{o.expectancy:,.0f} | — |",
            f"| Fees | ₩{_v(m['fees_paid']):,.0f} | ₩{o.fees:,.0f} | — |",
            f"| Exposure | {_v(m['exposure_pct']):.1f}% | {o.exposure_pct:.1f}% | 100% |",
            f"| Profitable OOS windows | — | {o.profitable_windows}/{o.windows} | — |",
            "",
            "Walk-forward windows (train→test):",
            "",
            "| train | test_end | chosen | train% | test% | testMDD% | trades |",
            "|---|---|---|---|---|---|---|",
        ]
        for w in r.walk_forward["windows"]:
            lines.append(
                f"| {w['train_start'][:10]}→{w['train_end'][5:10]} | {w['test_end'][:10]} | {w['best_params']} | {_v(w['train_return_pct']):+.2f} | {_v(w['test_return_pct']):+.2f} | {_v(w['test_mdd_pct']):.2f} | {w['test_trades']} |"
            )
        lines += [
            "",
            f"Parameter stability: neighbours positive-expectancy ratio {r.stability['neighbour_positive_ratio']:.0%} over {r.stability['neighbours_evaluated']} evaluations, cliff windows {r.stability['cliff_windows']}",
            "",
            "Cost stress (OOS):",
            "",
            "| costs | return% | PF | MDD% | trades |",
            "|---|---|---|---|---|",
        ]
        for k, v in r.cost_stress.items():
            lines.append(
                f"| {k} | {v['oos_return_pct']:+.2f} | {v['oos_pf']:.2f} | {v['oos_mdd_pct']:.2f} | {v['trades']} |"
            )
        lines += ["", "Approval matrix:", ""]
        for k, ok in r.matrix.items():
            lines.append(f"- {'PASS' if ok else 'FAIL'}  {k}")
        lines += [
            "",
            f"**{'APPROVED' if r.approved else 'NOT APPROVED'}**"
            + (f" — {'; '.join(r.notes)}" if r.notes else ""),
            "",
        ]
    approved = [r.spec.key for r in reports if r.approved]
    lines += [
        "---",
        f"# Result: {('CANDIDATE ' + ', '.join(approved)) if approved else 'NO_STRATEGY_APPROVED'}",
        "",
    ]
    return "\n".join(lines)


def default_specs() -> list[StrategySpec]:
    common = {"rsi_period": 14, "atr_period": 14, "volume_sma_period": 20, "trend_ema": 200}
    return [
        StrategySpec(
            key="A",
            name="trend_pullback",
            timeframe="15m",
            trend_timeframe="1h",
            base_params={
                **common,
                "ema_fast": 20,
                "ema_slow": 50,
                "pullback_rsi": 40,
                "arm_bars": 8,
                "rsi_exit_low": 45,
                "atr_stop_mult": 2.5,
                "atr_trail_mult": 3.5,
            },
            grid={
                "pullback_rsi": [35, 40, 45],
                "ema_slow": [50, 100],
                "atr_stop_mult": [2.0, 3.0],
                "atr_trail_mult": [3.0, 5.0],
            },
            description="1h close > EMA200 trend; 15m RSI pullback below threshold then EMA-fast re-cross entry; ATR stop/trail; exit on close < EMA slow",
        ),
        StrategySpec(
            key="B",
            name="donchian_breakout",
            timeframe="15m",
            trend_timeframe="1h",
            base_params={
                **common,
                "ema_fast": 20,
                "ema_slow": 50,
                "entry_n": 40,
                "exit_n": 20,
                "atr_stop_mult": 3.0,
                "atr_trail_mult": 4.0,
                "use_trend_filter": 1,
            },
            grid={
                "entry_n": [20, 40, 80],
                "exit_n": [10, 20],
                "atr_stop_mult": [2.0, 3.0],
                "use_trend_filter": [0, 1],
            },
            description="15m close above N-bar Donchian high (optional 1h EMA200 filter); exit below M-bar low; ATR stop/trail",
        ),
        StrategySpec(
            key="C",
            name="volatility_breakout",
            timeframe="15m",
            trend_timeframe="4h",
            base_params={
                **common,
                "ema_fast": 20,
                "ema_slow": 50,
                "k": 0.5,
                "hold_bars": 1,
                "atr_stop_mult": 2.0,
                "atr_trail_mult": 3.0,
                "use_trend_filter": 1,
            },
            grid={
                "k": [0.4, 0.5, 0.6],
                "hold_bars": [1, 2],
                "atr_stop_mult": [1.5, 2.5],
                "use_trend_filter": [0, 1],
            },
            description="4h open + k×previous-4h-range breakout (15m trigger, once per 4h bar, optional 4h EMA200 filter); time exit after hold_bars 4h bars; ATR stop/trail",
        ),
    ]


__all__ = [
    "ApprovalCriteria",
    "StrategyReport",
    "StrategySpec",
    "default_specs",
    "evaluate_strategy",
    "format_report",
    "itertools",
]
