"""Exit-round evaluation: fixed-parameter WF (same windows as Round 2/3), full period, recent 2Y,
2026 YTD, cost stress, concentration, and the exit-specific metrics (profit-to-loss reversals,
giveback, holding, winner preservation, trade matching against E0)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.engine import BacktestConfig
from app.backtest.metrics import TradeRecord
from app.backtest.research import oos_summary, with_cost_multiplier
from app.backtest.research2 import concentration
from app.backtest.walk_forward import WalkForwardResult, WindowResult
from app.core.types import Candle
from app.research.exit_round.engine import ExitResearchEngine, ExitResult, ExitRule


def _score(res: ExitResult) -> float:
    m = res.metrics
    return float(m.net_return_pct) / max(float(m.max_drawdown_pct), 1.0)


def _slice(candles: list[Candle], start: datetime, end: datetime) -> list[Candle]:
    return [c for c in candles if start <= c.open_time < end]


def run_wf(
    rule: ExitRule,
    params: dict[str, float],
    candles: list[Candle],
    cfg: BacktestConfig,
    *,
    train_days: int,
    test_days: int,
    warmup_days: int,
) -> tuple[WalkForwardResult, list[dict[str, Any]]]:
    """Same window arithmetic as app.backtest.walk_forward / round3 fixed WF; also returns the OOS
    position records (with MFE/MAE) of every test window."""
    start, end = candles[0].open_time, candles[-1].open_time
    windows: list[WindowResult] = []
    stitched: list[Decimal] = []
    oos_curve: list[Decimal] = []
    oos_positions: list[dict[str, Any]] = []
    eq = cfg.initial_equity
    t0 = start
    while t0 + timedelta(days=train_days + test_days) <= end + timedelta(minutes=1):
        t1 = t0 + timedelta(days=train_days)
        t2 = t1 + timedelta(days=test_days)
        warm = timedelta(days=warmup_days)
        if len(_slice(candles, t0, t1)) < 100 or len(_slice(candles, t1, t2)) < 20:
            break
        train_res = ExitResearchEngine(rule, params, cfg).run(
            _slice(candles, t0 - warm, t1), start_at=t0
        )
        test_res = ExitResearchEngine(rule, params, cfg).run(
            _slice(candles, t1 - warm, t2), start_at=t1
        )
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
                train_score=_score(train_res),
                test_score=_score(test_res),
                test_trades=list(test_res.trades),
                test_buy_hold_return_pct=bh,
            )
        )
        oos_positions.extend(p.to_dict() for p in test_res.positions)
        mult = test_res.metrics.ending_equity / test_res.metrics.starting_equity
        stitched.append(mult)
        for _, e in test_res.equity_curve:
            oos_curve.append(eq * (e / test_res.metrics.starting_equity))
        eq = eq * mult
        t0 = t0 + timedelta(days=test_days)
    total = Decimal(1)
    for m in stitched:
        total *= m
    hwm, mdd = cfg.initial_equity, Decimal(0)
    for e in oos_curve:
        hwm = max(hwm, e)
        mdd = max(mdd, (hwm - e) / hwm) if hwm > 0 else mdd
    wf = WalkForwardResult(
        windows=windows,
        grid_size=1,
        stitched_test_return_pct=(total - 1) * 100,
        stitched_test_mdd_pct=mdd * 100,
        oos_profitable_windows=sum(
            1 for w in windows if Decimal(w.test_metrics["net_return_pct"]) > 0
        ),
        param_stability={"fixed": len(windows)},
    )
    return wf, oos_positions


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[i]


def reversal_stats(positions: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [p for p in positions if p["exit_time"]]
    n = len(closed)
    loss = [p for p in closed if p["net_pnl"] < 0]

    def cnt(pred: Any) -> int:
        return sum(1 for p in loss if pred(p))

    a = cnt(lambda p: p["mfe_r"] > 0)
    b = cnt(lambda p: p["mfe_r"] >= 0.5)
    c = cnt(lambda p: p["mfe_r"] >= 1.0)
    d = cnt(lambda p: p["mfe_r"] >= 2.0)
    gb = [p["giveback_r"] for p in closed if p.get("giveback_r") is not None]
    gb_win = [
        p["giveback_r"] for p in closed if p.get("giveback_r") is not None and p["mfe_r"] >= 1.0
    ]
    hold = [p["holding_minutes"] for p in closed if p["holding_minutes"] is not None]
    return {
        "trades": n,
        "losing_trades": len(loss),
        "profit_to_loss": {"count": a, "ratio": a / n if n else 0.0},
        "meaningful_profit_to_loss_0_5R": {"count": b, "ratio": b / n if n else 0.0},
        "reached_1R_then_loss": {"count": c, "ratio": c / n if n else 0.0},
        "reached_2R_then_loss": {"count": d, "ratio": d / n if n else 0.0},
        "reached_1R": sum(1 for p in closed if p["mfe_r"] >= 1.0),
        "reached_2R": sum(1 for p in closed if p["mfe_r"] >= 2.0),
        "giveback_r": {
            "mean": (sum(gb) / len(gb)) if gb else None,
            "median": _pct(gb, 0.5),
            "p90": _pct(gb, 0.9),
            "n": len(gb),
        },
        "giveback_r_after_1R": {
            "mean": (sum(gb_win) / len(gb_win)) if gb_win else None,
            "median": _pct(gb_win, 0.5),
            "p90": _pct(gb_win, 0.9),
            "n": len(gb_win),
        },
        "holding_minutes": {
            "mean": (sum(hold) / len(hold)) if hold else None,
            "median": _pct([float(h) for h in hold], 0.5),
        },
        "avg_realized_r": (sum(p["realized_r"] for p in closed) / n) if n else None,
        "avg_mfe_r": (sum(p["mfe_r"] for p in closed) / n) if n else None,
    }


def _period(
    rule: ExitRule,
    params: dict[str, float],
    candles: list[Candle],
    cfg: BacktestConfig,
    start_at: datetime | None,
    warm_days: int,
) -> dict[str, Any]:
    sub = [
        c
        for c in candles
        if start_at is None or c.open_time >= start_at - timedelta(days=warm_days)
    ]
    if not sub or (start_at is not None and not any(c.open_time >= start_at for c in sub)):
        return {"available": False}
    res: ExitResult = ExitResearchEngine(rule, params, cfg).run(sub, start_at=start_at)
    m = res.metrics
    pos = [p.to_dict() for p in res.positions]
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
        "average_win": float(m.average_win),
        "average_loss": float(m.average_loss),
        "payoff_ratio": m.payoff_ratio,
        "exposure_pct": m.exposure_pct,
        "fees": float(m.fees_paid),
        "sharpe": m.sharpe_ratio,
        "max_consecutive_losses": m.max_consecutive_losses,
        "buy_hold_return_pct": float(m.buy_hold_return_pct),
        "cagr_pct": _cagr(float(m.net_return_pct), res.start, res.end),
        "positions": pos,
        "reversal": reversal_stats(pos),
    }


def _cagr(ret_pct: float, start: datetime, end: datetime) -> float | None:
    years = (end - start).total_seconds() / (365.25 * 86400)
    if years <= 0 or ret_pct <= -100:
        return None
    return float(((1 + ret_pct / 100) ** (1 / years) - 1) * 100)


def evaluate_rule(
    rule: ExitRule,
    params: dict[str, float],
    candles: list[Candle],
    cfg: BacktestConfig,
    *,
    start: datetime,
    end: datetime,
    train_days: int,
    test_days: int,
    warmup_days: int,
    recent_2y_start: datetime,
    ytd_start: datetime,
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
) -> dict[str, Any]:
    data = [c for c in candles if start <= c.open_time < end]
    full = _period(rule, params, data, cfg, None, 0)
    kw = dict(train_days=train_days, test_days=test_days, warmup_days=warmup_days)
    wf, oos_pos = run_wf(rule, params, data, cfg, **kw)
    oos = oos_summary(wf, data)
    oos_trades: list[TradeRecord] = [t for w in wf.windows for t in w.test_trades]
    conc = concentration(oos_trades, float(cfg.initial_equity))
    stress: dict[str, dict[str, Any]] = {}
    for m in cost_multipliers:
        if m == 1.0:
            om = oos
        else:
            wfm, _ = run_wf(rule, params, data, with_cost_multiplier(cfg, m), **kw)
            om = oos_summary(wfm, data)
        stress[f"{m}x"] = {
            "oos_return_pct": om.net_return_pct,
            "oos_pf": om.profit_factor,
            "oos_mdd_pct": om.max_drawdown_pct,
            "trades": om.trades,
        }
    windows = [
        {
            "train_end": w.train_end.isoformat(),
            "test_end": w.test_end.isoformat(),
            "test_return_pct": float(w.test_metrics["net_return_pct"]),
            "test_mdd_pct": float(w.test_metrics["max_drawdown_pct"]),
            "test_trades": int(w.test_metrics["trade_count"]),
            "test_bh_return_pct": w.test_buy_hold_return_pct,
        }
        for w in wf.windows
    ]
    best = max(windows, key=lambda w: float(str(w["test_return_pct"]))) if windows else None
    worst = min(windows, key=lambda w: float(str(w["test_return_pct"]))) if windows else None
    return {
        "rule": rule.to_dict(),
        "dataset": {
            "first": data[0].open_time.isoformat(),
            "last": data[-1].open_time.isoformat(),
            "bars_1h": len(data),
        },
        "wf_setup": {
            "train_days": train_days,
            "test_days": test_days,
            "roll_days": test_days,
            "warmup_days": warmup_days,
            "windows": len(wf.windows),
        },
        "oos": {
            "net_return_pct": oos.net_return_pct,
            "max_drawdown_pct": oos.max_drawdown_pct,
            "profit_factor": oos.profit_factor,
            "trades": oos.trades,
            "win_rate": oos.win_rate,
            "expectancy": oos.expectancy,
            "exposure_pct": oos.exposure_pct,
            "fees": oos.fees,
            "profitable_windows": oos.profitable_windows,
            "losing_windows": oos.windows - oos.profitable_windows,
            "windows": oos.windows,
            "best_window": best,
            "worst_window": worst,
            "buy_hold_return_pct": oos.buy_hold_return_pct,
        },
        "oos_reversal": reversal_stats(oos_pos),
        "windows": windows,
        "cost_stress": stress,
        "concentration": {
            "top1_contribution": conc.top1_contribution,
            "top5_contribution": conc.top5_contribution,
            "return_pct_ex_top5": conc.return_pct_ex_top5,
            "return_pct_ex_top1": conc.return_pct_ex_top1,
            "concentration_risk": conc.concentration_risk,
        },
        "full_period": full,
        "recent_2y": _period(rule, params, data, cfg, recent_2y_start, warmup_days),
        "ytd_2026": _period(rule, params, data, cfg, ytd_start, warmup_days),
    }


def match_trades(control: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    """Trade-matched comparison on the full period: same entry (identical entry time) => compare exits."""
    c0 = {p["entry_time"]: p for p in control["full_period"]["positions"]}
    c1 = {p["entry_time"]: p for p in cand["full_period"]["positions"]}
    common = sorted(set(c0) & set(c1))
    rows = []
    for k in common:
        a, b = c0[k], c1[k]
        rows.append(
            {
                "entry_time": k,
                "entry_price": a["entry_price"],
                "r_pct": a["r_pct"],
                "e0_exit_time": a["exit_time"],
                "e0_exit_reason": a["exit_reason"],
                "e0_pnl": a["net_pnl"],
                "e0_return_pct": a["return_pct"],
                "e0_hold_min": a["holding_minutes"],
                "e0_mfe_r": a["mfe_r"],
                "e0_mae_r": a["mae_r"],
                "c_exit_time": b["exit_time"],
                "c_exit_reason": b["exit_reason"],
                "c_pnl": b["net_pnl"],
                "c_return_pct": b["return_pct"],
                "c_hold_min": b["holding_minutes"],
                "c_mfe_r": b["mfe_r"],
                "c_mae_r": b["mae_r"],
                "pnl_diff": b["net_pnl"] - a["net_pnl"],
                "earlier_exit": (b["exit_time"] or "") < (a["exit_time"] or ""),
            }
        )
    e0_pnl = sum(r["e0_pnl"] for r in rows)
    c_pnl = sum(r["c_pnl"] for r in rows)
    top10 = sorted(rows, key=lambda r: -r["e0_pnl"])[:10]
    big = [r for r in rows if r["e0_pnl"] > 0]
    big_pos = sum(r["e0_pnl"] for r in big) or 1.0
    return {
        "matched": len(rows),
        "e0_only": len(set(c0) - set(c1)),
        "cand_only": len(set(c1) - set(c0)),
        "matched_e0_pnl": e0_pnl,
        "matched_cand_pnl": c_pnl,
        "upside_capture_ratio": (c_pnl / e0_pnl) if e0_pnl else None,
        "winner_capture_ratio": sum(r["c_pnl"] for r in big) / big_pos,
        "earlier_exits": sum(1 for r in rows if r["earlier_exit"]),
        "same_exit": sum(1 for r in rows if r["c_exit_time"] == r["e0_exit_time"]),
        "top10_e0_winners": [
            {
                k: v
                for k, v in r.items()
                if k
                in (
                    "entry_time",
                    "e0_pnl",
                    "c_pnl",
                    "pnl_diff",
                    "e0_exit_reason",
                    "c_exit_reason",
                    "e0_hold_min",
                    "c_hold_min",
                    "e0_mfe_r",
                )
            }
            for r in top10
        ],
        "top10_pnl_diff_total": sum(r["pnl_diff"] for r in top10),
        "top10_cut_early": sum(1 for r in top10 if r["earlier_exit"]),
        "representative": sorted(rows, key=lambda r: -abs(r["pnl_diff"]))[:10],
        "mean_hold_min_e0": (sum(r["e0_hold_min"] or 0 for r in rows) / len(rows))
        if rows
        else None,
        "mean_hold_min_cand": (sum(r["c_hold_min"] or 0 for r in rows) / len(rows))
        if rows
        else None,
    }


def classify(
    control: dict[str, Any], cand: dict[str, Any], match: dict[str, Any]
) -> tuple[str, list[str]]:
    """Pre-registered reading of §15: MDD not worse (or better), PF not seriously lower (>= 85% of
    E0), expectancy kept (>= 85%), meaningful P->L reversals reduced, recent 2Y not collapsed,
    WF windows kept, cost stress kept, winners not cut too much (top-10 winner PnL >= 85% of E0)."""
    o0, o1 = control["oos"], cand["oos"]
    r0, r1 = control["full_period"]["reversal"], cand["full_period"]["reversal"]
    notes: list[str] = []
    checks = {
        "mdd_not_worse": o1["max_drawdown_pct"] <= o0["max_drawdown_pct"] + 0.25,
        "pf_kept": o1["profit_factor"] >= 0.85 * o0["profit_factor"],
        "expectancy_kept": o1["expectancy"] >= 0.85 * o0["expectancy"],
        "reversal_reduced": r1["meaningful_profit_to_loss_0_5R"]["count"]
        < r0["meaningful_profit_to_loss_0_5R"]["count"],
        "recent_2y_ok": cand["recent_2y"]["net_return_pct"] > 0
        and cand["recent_2y"]["net_return_pct"] >= 0.5 * control["recent_2y"]["net_return_pct"],
        "wf_windows_kept": o1["profitable_windows"] >= o0["profitable_windows"] - 1,
        "cost_stress_kept": cand["cost_stress"]["2.0x"]["oos_return_pct"] > 0,
        "winners_kept": (
            sum(r["c_pnl"] for r in match["top10_e0_winners"])
            >= 0.85 * sum(r["e0_pnl"] for r in match["top10_e0_winners"])
        )
        if match["top10_e0_winners"]
        else True,
        "return_not_collapsed": o1["net_return_pct"] >= 0.7 * o0["net_return_pct"],
    }
    failed = [k for k, v in checks.items() if not v]
    if not failed:
        verdict = "PROMISING"
    elif (
        len(failed) <= 2
        and checks["reversal_reduced"]
        and checks["recent_2y_ok"]
        and checks["cost_stress_kept"]
    ):
        verdict = "MIXED"
    else:
        verdict = "REJECT"
    notes += [f"FAIL {k}" for k in failed]
    return verdict, notes
