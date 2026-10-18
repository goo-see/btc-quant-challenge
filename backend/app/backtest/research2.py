"""Research round 2: low-frequency trend following (D 4h / E 1d) on the long 1h history.

Adds to round 1: warm-up prefixes, OOS trade-contribution analysis, per-window regime (B&H sign),
and a three-way verdict: APPROVED (>=50 OOS trades + all criteria) / REVIEW (30..49 trades or
concentration risk, everything else passing) / NOT_APPROVED. Criteria are NOT relaxed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.metrics import TradeRecord
from app.backtest.research import (
    ApprovalCriteria,
    OosSummary,
    StrategySpec,
    oos_summary,
    stability_check,
    with_cost_multiplier,
)
from app.backtest.walk_forward import WalkForwardResult, run_walk_forward
from app.core.types import Candle
from app.strategy.registry import build_strategy

REVIEW_MIN_TRADES = 30


@dataclass(slots=True)
class Concentration:
    trades: int
    total_net_pnl: float
    largest_win: float
    median_pnl: float
    top1_contribution: float  # share of total net pnl (or of gross profit when total <= 0)
    top3_contribution: float
    top5_contribution: float
    return_pct_ex_top1: float
    return_pct_ex_top3: float
    return_pct_ex_top5: float
    concentration_risk: bool
    detail: str = ""


def concentration(trades: list[TradeRecord], initial_equity: float) -> Concentration:
    pnls = sorted((float(t.net_pnl) for t in trades), reverse=True)
    n = len(pnls)
    if n == 0:
        return Concentration(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, "no trades")
    total = sum(pnls)
    denom = total if total > 0 else sum(p for p in pnls if p > 0) or 1.0

    def top(k: int) -> float:
        return sum(pnls[:k])

    def ex(k: int) -> float:
        return (total - top(k)) / initial_equity * 100

    median = (
        sorted(pnls)[n // 2] if n % 2 else (sorted(pnls)[n // 2 - 1] + sorted(pnls)[n // 2]) / 2
    )
    risk = total > 0 and (ex(1) <= 0 or ex(3) <= 0)
    return Concentration(
        trades=n,
        total_net_pnl=total,
        largest_win=pnls[0],
        median_pnl=median,
        top1_contribution=top(1) / denom,
        top3_contribution=top(3) / denom,
        top5_contribution=top(5) / denom,
        return_pct_ex_top1=ex(1),
        return_pct_ex_top3=ex(3),
        return_pct_ex_top5=ex(5),
        concentration_risk=risk,
        detail=("total flips negative without top-1/top-3 trades" if risk else ""),
    )


@dataclass(slots=True)
class Round2Report:
    spec: StrategySpec
    dataset: dict[str, Any]
    wf_setup: dict[str, Any]
    full_backtest: dict[str, Any]
    windows: list[dict[str, Any]]
    oos: OosSummary
    stability: dict[str, Any]
    cost_stress: dict[str, dict[str, Any]]
    concentration: Concentration
    regimes: dict[str, Any]
    matrix: dict[str, bool]
    verdict: str  # APPROVED | REVIEW | NOT_APPROVED
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _regimes(wf: WalkForwardResult) -> dict[str, Any]:
    bull = [w for w in wf.windows if w.test_buy_hold_return_pct > 0]
    bear = [w for w in wf.windows if w.test_buy_hold_return_pct <= 0]

    def _avg(ws: list[Any]) -> float:
        return sum(float(w.test_metrics["net_return_pct"]) for w in ws) / len(ws) if ws else 0.0

    return {
        "windows": len(wf.windows),
        "bull_windows": len(bull),
        "bear_windows": len(bear),
        "avg_oos_return_bull": _avg(bull),
        "avg_oos_return_bear": _avg(bear),
        "profitable_in_bull": sum(1 for w in bull if float(w.test_metrics["net_return_pct"]) > 0),
        "profitable_in_bear": sum(1 for w in bear if float(w.test_metrics["net_return_pct"]) > 0),
        "years": sorted({w.train_end.year for w in wf.windows}),
    }


def evaluate_round2(
    spec: StrategySpec,
    candles: list[Candle],
    cfg: BacktestConfig,
    *,
    train_days: int,
    test_days: int,
    warmup_days: int,
    criteria: ApprovalCriteria,
    dataset: dict[str, Any],
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
) -> Round2Report:
    full = BacktestEngine(
        build_strategy(spec.name, spec.timeframe, spec.trend_timeframe, spec.base_params), cfg
    ).run(candles)
    kw = dict(
        candles=candles,
        strategy_name=spec.name,
        timeframe=spec.timeframe,
        trend_timeframe=spec.trend_timeframe,
        base_params=spec.base_params,
        grid=spec.grid,
        train_days=train_days,
        test_days=test_days,
        warmup_days=warmup_days,
        min_trades=3,
    )
    wf = run_walk_forward(cfg=cfg, **kw)  # type: ignore[arg-type]
    oos = oos_summary(wf, candles)
    stab = stability_check(spec, candles, wf, cfg)
    oos_trades = [t for w in wf.windows for t in w.test_trades]
    conc = concentration(oos_trades, float(cfg.initial_equity))
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
        wfm = run_walk_forward(cfg=with_cost_multiplier(cfg, m), **kw)  # type: ignore[arg-type]
        om = oos_summary(wfm, candles)
        stress[f"{m}x"] = {
            "oos_return_pct": om.net_return_pct,
            "oos_pf": om.profit_factor,
            "oos_mdd_pct": om.max_drawdown_pct,
            "trades": om.trades,
        }
    base_ret = oos.net_return_pct
    s15 = stress.get("1.5x", {}).get("oos_return_pct", base_ret)
    cost_ok = base_ret > 0 and s15 > 0 and s15 >= base_ret * criteria.max_cost_stress_collapse
    win_ratio = (oos.profitable_windows / oos.windows) if oos.windows else 0.0
    matrix = {
        "oos_return_gt_0": oos.net_return_pct > criteria.min_oos_return_pct,
        "oos_pf_ge_1.20": oos.profit_factor >= criteria.min_oos_profit_factor,
        "oos_mdd_le_10": oos.max_drawdown_pct <= criteria.max_oos_mdd_pct,
        "profitable_windows_ge_60pct": win_ratio >= criteria.min_profitable_window_ratio,
        "oos_trades_ge_50": oos.trades >= criteria.min_oos_trades,
        "oos_trades_ge_30_review": oos.trades >= REVIEW_MIN_TRADES,
        "parameter_stability": stab["neighbour_positive_ratio"]
        >= criteria.min_neighborhood_positive_ratio
        and stab["cliff_windows"] == 0,
        "cost_stress_1.5x": cost_ok,
        "no_concentration_risk": not conc.concentration_risk,
    }
    core = [
        "oos_return_gt_0",
        "oos_pf_ge_1.20",
        "oos_mdd_le_10",
        "profitable_windows_ge_60pct",
        "parameter_stability",
        "cost_stress_1.5x",
    ]
    core_ok = all(matrix[k] for k in core)
    if core_ok and matrix["oos_trades_ge_50"] and matrix["no_concentration_risk"]:
        verdict = "APPROVED"
    elif core_ok and matrix["oos_trades_ge_30_review"]:
        verdict = "REVIEW"
    else:
        verdict = "NOT_APPROVED"
    notes: list[str] = []
    if stab["cliff_windows"]:
        notes.append(f"parameter cliff in {stab['cliff_windows']} window(s)")
    if conc.concentration_risk:
        notes.append("concentration risk: " + conc.detail)
    if base_ret > 0 and not cost_ok:
        notes.append("collapses under 1.5x costs")
    if verdict == "REVIEW":
        notes.append(f"REVIEW: {oos.trades} OOS trades (<50) — not approvable on trade count alone")
    return Round2Report(
        spec=spec,
        dataset=dataset,
        wf_setup={
            "train_days": train_days,
            "test_days": test_days,
            "warmup_days": warmup_days,
            "roll_days": test_days,
            "grid_size": math.prod(len(v) for v in spec.grid.values()),
            "windows": len(wf.windows),
        },
        full_backtest={**full.metrics.to_dict(), "signals": full.signals},
        windows=[
            {
                "train_start": w.train_start.isoformat(),
                "train_end": w.train_end.isoformat(),
                "test_end": w.test_end.isoformat(),
                "best_params": w.best_params,
                "train_return_pct": float(w.train_metrics["net_return_pct"]),
                "test_return_pct": float(w.test_metrics["net_return_pct"]),
                "test_mdd_pct": float(w.test_metrics["max_drawdown_pct"]),
                "test_trades": int(w.test_metrics["trade_count"]),
                "test_bh_return_pct": w.test_buy_hold_return_pct,
            }
            for w in wf.windows
        ],
        oos=oos,
        stability=stab,
        cost_stress=stress,
        concentration=conc,
        regimes=_regimes(wf),
        matrix=matrix,
        verdict=verdict,
        notes=notes,
    )


def format_round2(reports: list[Round2Report]) -> str:
    lines: list[str] = []
    for r in reports:
        m, o, c = r.full_backtest, r.oos, r.concentration
        lines += [
            f"## {r.spec.key}. {r.spec.name} — {r.spec.description}",
            f"TF {r.spec.timeframe} / trend {r.spec.trend_timeframe} · base {r.spec.base_params}",
            f"grid {r.spec.grid} ({r.wf_setup['grid_size']} combos) · WF train {r.wf_setup['train_days']}d / test {r.wf_setup['test_days']}d / warm-up {r.wf_setup['warmup_days']}d · {r.wf_setup['windows']} windows",
            "",
            "| metric | FULL (base params) | OOS (stitched) | Buy & Hold |",
            "|---|---|---|---|",
            f"| Net return | {float(m['net_return_pct']):+.2f}% | {o.net_return_pct:+.2f}% | {o.buy_hold_return_pct:+.2f}% |",
            f"| Max drawdown | {float(m['max_drawdown_pct']):.2f}% | {o.max_drawdown_pct:.2f}% | {o.buy_hold_mdd_pct:.2f}% |",
            f"| Trades | {m['trade_count']} | {o.trades} | — |",
            f"| Profit factor | {float(m['profit_factor']):.2f} | {o.profit_factor:.2f} | — |",
            f"| Win rate | {float(m['win_rate']) * 100:.1f}% | {o.win_rate * 100:.1f}% | — |",
            f"| Expectancy | ₩{float(m['expectancy']):,.0f} | ₩{o.expectancy:,.0f} | — |",
            f"| Fees | ₩{float(m['fees_paid']):,.0f} | ₩{o.fees:,.0f} | — |",
            f"| Exposure | {float(m['exposure_pct']):.1f}% | {o.exposure_pct:.1f}% | 100% |",
            f"| Profitable OOS windows | — | {o.profitable_windows}/{o.windows} | — |",
            "",
            "| train | test_end | chosen | train% | test% | testMDD% | trades | B&H% |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for w in r.windows:
            lines.append(
                f"| {w['train_start'][:10]}→{w['train_end'][2:10]} | {w['test_end'][:10]} | {w['best_params']} | {w['train_return_pct']:+.2f} | {w['test_return_pct']:+.2f} | {w['test_mdd_pct']:.2f} | {w['test_trades']} | {w['test_bh_return_pct']:+.1f} |"
            )
        rg = r.regimes
        lines += [
            "",
            f"Regimes: {rg['bull_windows']} bull / {rg['bear_windows']} bear test windows (by B&H sign); avg OOS return bull {rg['avg_oos_return_bull']:+.2f}% (profitable {rg['profitable_in_bull']}/{rg['bull_windows']}), bear {rg['avg_oos_return_bear']:+.2f}% (profitable {rg['profitable_in_bear']}/{rg['bear_windows']}); years {rg['years']}",
            f"Parameter stability: neighbour positive-expectancy {r.stability['neighbour_positive_ratio']:.0%} over {r.stability['neighbours_evaluated']} evaluations, cliff windows {r.stability['cliff_windows']}",
            "",
            "| costs | OOS return% | PF | MDD% | trades |",
            "|---|---|---|---|---|",
        ]
        for k, v in r.cost_stress.items():
            lines.append(
                f"| {k} | {v['oos_return_pct']:+.2f} | {v['oos_pf']:.2f} | {v['oos_mdd_pct']:.2f} | {v['trades']} |"
            )
        lines += [
            "",
            f"Trade concentration (OOS, {c.trades} trades): largest win ₩{c.largest_win:,.0f}, median ₩{c.median_pnl:,.0f}, total ₩{c.total_net_pnl:,.0f}; top1/3/5 contribution {c.top1_contribution:.0%}/{c.top3_contribution:.0%}/{c.top5_contribution:.0%}; return ex-top1/3/5 {c.return_pct_ex_top1:+.2f}% / {c.return_pct_ex_top3:+.2f}% / {c.return_pct_ex_top5:+.2f}% → {'CONCENTRATION RISK' if c.concentration_risk else 'ok'}",
            "",
            "Approval matrix:",
            "",
        ]
        for k, ok in r.matrix.items():
            lines.append(f"- {'PASS' if ok else 'FAIL'}  {k}")
        lines += ["", f"**{r.verdict}**" + (f" — {'; '.join(r.notes)}" if r.notes else ""), ""]
    approved = [r.spec.key for r in reports if r.verdict == "APPROVED"]
    review = [r.spec.key for r in reports if r.verdict == "REVIEW"]
    lines += [
        "---",
        "# Result: "
        + (
            f"CANDIDATE {', '.join(approved)}"
            if approved
            else (f"REVIEW {', '.join(review)}" if review else "NO_STRATEGY_APPROVED")
        ),
        "",
    ]
    return "\n".join(lines)


def round2_specs() -> list[StrategySpec]:
    common = {
        "rsi_period": 14,
        "atr_period": 14,
        "volume_sma_period": 20,
        "ema_fast": 20,
        "ema_slow": 50,
    }
    return [
        StrategySpec(
            key="D",
            name="trend_following_4h",
            timeframe="4h",
            trend_timeframe="1d",
            base_params={
                **common,
                "trend_ema": 50,
                "entry_n": 30,
                "exit_n": 15,
                "atr_stop_mult": 3.0,
                "atr_trail_mult": 4.0,
                "exit_on_trend_loss": 1,
            },
            grid={
                "entry_n": [20, 30, 42],
                "trend_ema": [50, 100],
                "atr_stop_mult": [2.0, 3.0],
                "exit_n": [10, 20],
            },
            description="1d close > EMA(trend) filter; 4h close above N-bar high entry; ATR stop/trail; exit below M-bar low or trend loss",
        ),
        StrategySpec(
            key="E",
            name="trend_following_1d",
            timeframe="1d",
            trend_timeframe="1d",
            base_params={
                **common,
                "trend_ema": 100,
                "entry_n": 40,
                "exit_n": 20,
                "atr_stop_mult": 3.0,
                "atr_trail_mult": 4.0,
                "exit_on_trend_loss": 1,
            },
            grid={
                "entry_n": [20, 40, 55],
                "trend_ema": [100, 200],
                "atr_stop_mult": [2.0, 3.0],
                "exit_n": [10, 20],
            },
            description="daily close > EMA(trend) filter; N-day breakout entry; ATR stop/trail; exit below M-day low or trend loss",
        ),
    ]
