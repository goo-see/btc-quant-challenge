"""Round 3 historical evaluation — fixed-parameter WF on the 1h dataset, identical for D0..D6.

Deterministic: the same candles + the same StrategyDef always give the same dict (no RNG, no
wall-clock inputs). Safe to run in separate processes; results are merged by strategy_id.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from app.backtest.metrics import TradeRecord
from app.backtest.research import oos_summary, with_cost_multiplier
from app.backtest.research2 import concentration
from app.backtest.walk_forward import WalkForwardResult, WindowResult, score
from app.core.types import Candle
from app.research.round3.config import GateSpec, HistoricalSpec, StrategyDef


def _slice(candles: list[Candle], start: datetime, end: datetime) -> list[Candle]:
    return [c for c in candles if start <= c.open_time < end]


def run_fixed_walk_forward(
    sd: StrategyDef,
    candles: list[Candle],
    cfg: BacktestConfig,
    *,
    train_days: int,
    test_days: int,
    warmup_days: int,
) -> WalkForwardResult:
    """app.backtest.walk_forward.run_walk_forward with FIXED parameters and a direct strategy
    factory (D1..D6 are not in the trader registry — the registry is left untouched).
    Window boundaries, warm-up prefix, per-window ₩ reset and stitched OOS metrics are identical."""
    start, end = candles[0].open_time, candles[-1].open_time
    windows: list[WindowResult] = []
    stitched: list[Decimal] = []
    oos_curve: list[Decimal] = []
    eq = cfg.initial_equity
    t0 = start
    while t0 + timedelta(days=train_days + test_days) <= end + timedelta(minutes=1):
        t1 = t0 + timedelta(days=train_days)
        t2 = t1 + timedelta(days=test_days)
        warm = timedelta(days=warmup_days)
        train = _slice(candles, t0 - warm, t1)
        test = _slice(candles, t1 - warm, t2)
        if len(_slice(candles, t0, t1)) < 100 or len(_slice(candles, t1, t2)) < 20:
            break
        train_res = BacktestEngine(sd.build(), cfg).run(train, start_at=t0)
        test_res = BacktestEngine(sd.build(), cfg).run(test, start_at=t1)
        test_live = _slice(candles, t1, t2)
        bh = (float(test_live[-1].close) / float(test_live[0].open) - 1) * 100 if test_live else 0.0
        windows.append(
            WindowResult(
                train_start=t0,
                train_end=t1,
                test_end=t2,
                best_params={},
                train_metrics=train_res.metrics.to_dict(),
                test_metrics=test_res.metrics.to_dict(),
                train_score=score(train_res, 0),
                test_score=score(test_res, 0),
                test_trades=list(test_res.trades),
                test_buy_hold_return_pct=bh,
            )
        )
        mult = test_res.metrics.ending_equity / test_res.metrics.starting_equity
        stitched.append(mult)
        for _, e in test_res.equity_curve:
            oos_curve.append(eq * (e / test_res.metrics.starting_equity))
        eq = eq * mult
        t0 = t0 + timedelta(days=test_days)
    total_mult = Decimal(1)
    for m in stitched:
        total_mult *= m
    hwm = cfg.initial_equity
    mdd = Decimal(0)
    for e in oos_curve:
        hwm = max(hwm, e)
        mdd = max(mdd, (hwm - e) / hwm) if hwm > 0 else mdd
    return WalkForwardResult(
        windows=windows,
        grid_size=1,
        stitched_test_return_pct=(total_mult - 1) * 100,
        stitched_test_mdd_pct=mdd * 100,
        oos_profitable_windows=sum(
            1 for w in windows if Decimal(w.test_metrics["net_return_pct"]) > 0
        ),
        param_stability={"fixed": len(windows)},
    )


def _period(
    sd: StrategyDef,
    candles: list[Candle],
    cfg: BacktestConfig,
    start_at: datetime | None,
    warm_days: int,
) -> dict[str, Any]:
    if start_at is not None:
        sub = [c for c in candles if c.open_time >= start_at - timedelta(days=warm_days)]
    else:
        sub = candles
    if not sub or (start_at is not None and not any(c.open_time >= start_at for c in sub)):
        return {"available": False}
    res: BacktestResult = BacktestEngine(sd.build(), cfg).run(sub, start_at=start_at)
    m = res.metrics
    return {
        "available": True,
        "start": res.start.isoformat(),
        "end": res.end.isoformat(),
        "net_return_pct": float(m.net_return_pct),
        "max_drawdown_pct": float(m.max_drawdown_pct),
        "profit_factor": m.profit_factor,
        "trades": m.trade_count,
        "win_rate": m.win_rate,
        "expectancy": float(m.expectancy),
        "exposure_pct": m.exposure_pct,
        "fees": float(m.fees_paid),
        "buy_hold_return_pct": float(m.buy_hold_return_pct),
        "max_consecutive_losses": m.max_consecutive_losses,
        "trades_list": [
            {
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "net_pnl": float(t.net_pnl),
                "return_pct": float(t.return_pct),
                "holding_minutes": t.holding_minutes,
                "exit_reason": t.exit_reason,
            }
            for t in res.trades
        ],
    }


def _fake_breakout(trades: list[dict[str, Any]], max_hours: int) -> dict[str, Any]:
    fake = [t for t in trades if t["net_pnl"] <= 0 and t["holding_minutes"] <= max_hours * 60]
    return {
        "definition": f"losing trade closed within {max_hours}h of entry",
        "count": len(fake),
        "rate": (len(fake) / len(trades)) if trades else 0.0,
        "stop_exits": sum(1 for t in trades if str(t["exit_reason"]).startswith("STOP")),
    }


def classify(metrics: dict[str, Any], gate: GateSpec) -> tuple[str, dict[str, bool], list[str]]:
    o = metrics["oos"]
    stress = metrics["cost_stress"]
    conc = metrics["concentration"]
    win_ratio = (o["profitable_windows"] / o["windows"]) if o["windows"] else 0.0
    checks = {
        "oos_return_gt_0": o["net_return_pct"] > gate.min_oos_return_pct,
        "oos_pf_ge_1.20": o["profit_factor"] >= gate.min_oos_profit_factor,
        "oos_mdd_le_10": o["max_drawdown_pct"] <= gate.max_oos_mdd_pct,
        "profitable_windows_ge_60pct": win_ratio >= gate.min_profitable_window_ratio,
        "cost_1.5x_positive": stress.get("1.5x", {}).get("oos_return_pct", 0.0) > 0,
        "oos_trades_ge_50": o["trades"] >= gate.min_oos_trades,
        "oos_trades_ge_30": o["trades"] >= gate.review_min_trades,
        "cost_2.0x_positive": stress.get("2.0x", {}).get("oos_return_pct", 0.0) > 0,
        "no_concentration_risk": not conc["concentration_risk"],
        "recent_2y_not_collapsed": metrics["recent_2y"].get("net_return_pct", -1.0)
        > gate.recent_2y_min_return_pct,
    }
    core = [
        "oos_return_gt_0",
        "oos_pf_ge_1.20",
        "oos_mdd_le_10",
        "profitable_windows_ge_60pct",
        "cost_1.5x_positive",
    ]
    notes: list[str] = []
    if not all(checks[k] for k in core) or not checks["oos_trades_ge_30"]:
        verdict = "REJECT"
        notes += [f"core FAIL: {k}" for k in core if not checks[k]]
        if not checks["oos_trades_ge_30"]:
            notes.append(f"OOS trades {o['trades']} < {gate.review_min_trades}")
    else:
        extra = [
            "oos_trades_ge_50",
            "cost_2.0x_positive",
            "no_concentration_risk",
            "recent_2y_not_collapsed",
        ]
        failed = [k for k in extra if not checks[k]]
        verdict = "PASS" if not failed else "REVIEW"
        notes += [f"REVIEW flag: {k}" for k in failed]
    return verdict, checks, notes


def evaluate_strategy(
    sd: StrategyDef,
    candles: list[Candle],
    cfg: BacktestConfig,
    hist: HistoricalSpec,
    gate: GateSpec,
) -> dict[str, Any]:
    data = [c for c in candles if hist.start <= c.open_time < hist.end]
    full = _period(sd, data, cfg, None, 0)
    kw: dict[str, int] = dict(
        train_days=hist.train_days, test_days=hist.test_days, warmup_days=hist.warmup_days
    )
    wf = run_fixed_walk_forward(sd, data, cfg, **kw)
    oos = oos_summary(wf, data)
    oos_trades: list[TradeRecord] = [t for w in wf.windows for t in w.test_trades]
    conc = concentration(oos_trades, float(cfg.initial_equity))
    stress: dict[str, dict[str, Any]] = {}
    for m in hist.cost_multipliers:
        if m == 1.0:
            wfm, om = wf, oos
        else:
            wfm = run_fixed_walk_forward(sd, data, with_cost_multiplier(cfg, m), **kw)
            om = oos_summary(wfm, data)
        stress[f"{m}x"] = {
            "oos_return_pct": om.net_return_pct,
            "oos_pf": om.profit_factor,
            "oos_mdd_pct": om.max_drawdown_pct,
            "trades": om.trades,
            "fees": om.fees,
        }
    windows: list[dict[str, Any]] = [
        {
            "train_start": w.train_start.isoformat(),
            "train_end": w.train_end.isoformat(),
            "test_end": w.test_end.isoformat(),
            "test_return_pct": float(w.test_metrics["net_return_pct"]),
            "test_mdd_pct": float(w.test_metrics["max_drawdown_pct"]),
            "test_trades": int(w.test_metrics["trade_count"]),
            "test_bh_return_pct": w.test_buy_hold_return_pct,
        }
        for w in wf.windows
    ]
    best = max(windows, key=lambda w: float(w["test_return_pct"])) if windows else None
    worst = min(windows, key=lambda w: float(w["test_return_pct"])) if windows else None
    metrics: dict[str, Any] = {
        "strategy": sd.to_dict(),
        "dataset": {
            "first": data[0].open_time.isoformat() if data else None,
            "last": data[-1].open_time.isoformat() if data else None,
            "bars_1h": len(data),
        },
        "wf_setup": {
            "train_days": hist.train_days,
            "test_days": hist.test_days,
            "roll_days": hist.roll_days,
            "warmup_days": hist.warmup_days,
            "windows": len(wf.windows),
            "fixed_params": True,
        },
        "oos": {
            "net_return_pct": oos.net_return_pct,
            "max_drawdown_pct": oos.max_drawdown_pct,
            "profit_factor": oos.profit_factor,
            "trades": oos.trades,
            "wins": oos.wins,
            "losses": oos.losses,
            "win_rate": oos.win_rate,
            "expectancy": oos.expectancy,
            "exposure_pct": oos.exposure_pct,
            "fees": oos.fees,
            "profitable_windows": oos.profitable_windows,
            "losing_windows": oos.windows - oos.profitable_windows,
            "windows": oos.windows,
            "buy_hold_return_pct": oos.buy_hold_return_pct,
            "buy_hold_mdd_pct": oos.buy_hold_mdd_pct,
            "best_window": best,
            "worst_window": worst,
        },
        "windows": windows,
        "cost_stress": stress,
        "concentration": {
            "trades": conc.trades,
            "total_net_pnl": conc.total_net_pnl,
            "largest_win": conc.largest_win,
            "median_pnl": conc.median_pnl,
            "top1_contribution": conc.top1_contribution,
            "top3_contribution": conc.top3_contribution,
            "top5_contribution": conc.top5_contribution,
            "return_pct_ex_top1": conc.return_pct_ex_top1,
            "return_pct_ex_top3": conc.return_pct_ex_top3,
            "return_pct_ex_top5": conc.return_pct_ex_top5,
            "concentration_risk": conc.concentration_risk,
            "detail": conc.detail,
        },
        "full_period": full,
        "recent_2y": _period(sd, data, cfg, hist.recent_2y_start, hist.warmup_days),
        "ytd_2026": _period(sd, data, cfg, hist.ytd_start, hist.warmup_days),
        "fake_breakout_full": _fake_breakout(
            full.get("trades_list", []), hist.fake_breakout_max_hours
        ),
        "fake_breakout_oos": _fake_breakout(
            [
                {
                    "net_pnl": float(t.net_pnl),
                    "holding_minutes": t.holding_minutes,
                    "exit_reason": t.exit_reason,
                }
                for t in oos_trades
            ],
            hist.fake_breakout_max_hours,
        ),
    }
    verdict, checks, notes = classify(metrics, gate)
    metrics["gate"] = {"verdict": verdict, "checks": checks, "notes": notes}
    return metrics


def timing_vs_control(control: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """For each control (D0) full-period entry: earliest `other` entry within [D0-5d, D0+1d]."""
    d0 = [
        datetime.fromisoformat(t["entry_time"])
        for t in control["full_period"].get("trades_list", [])
    ]
    dk = [
        datetime.fromisoformat(t["entry_time"]) for t in other["full_period"].get("trades_list", [])
    ]
    leads: list[float] = []
    matched = 0
    for e in d0:
        cands = [x for x in dk if e - timedelta(days=5) <= x <= e + timedelta(days=1)]
        if cands:
            matched += 1
            leads.append((e - min(cands)).total_seconds() / 3600)
    leads.sort()
    unmatched_other = sum(
        1 for x in dk if not any(x - timedelta(days=1) <= e <= x + timedelta(days=5) for e in d0)
    )
    return {
        "d0_entries": len(d0),
        "other_entries": len(dk),
        "d0_entries_matched": matched,
        "match_rate": (matched / len(d0)) if d0 else 0.0,
        "earlier_than_d0": sum(1 for x in leads if x > 0),
        "median_lead_hours": leads[len(leads) // 2] if leads else None,
        "mean_lead_hours": (sum(leads) / len(leads)) if leads else None,
        "other_entries_without_d0_entry": unmatched_other,
    }
