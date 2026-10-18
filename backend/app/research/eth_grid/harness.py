"""Historical evaluation of grid candidates on 5m ETH/KRW bars (+ daily EMA100 guard, 1h ATR for
G3), regimes, cost stress, recent windows and the D0+grid portfolio combination.
Total sleeve equity (cash + ETH mark-to-market − fees) is the performance measure."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.core.types import Candle, Timeframe
from app.market.candle_engine import resample_1m
from app.market.indicators import ATR, EMA
from app.research.eth_grid.engine import KST, GridEngine, GridParams


def run_grid(p: GridParams, bars: list[Candle], *, start_at: datetime | None = None) -> GridEngine:
    """Feed 5m bars; daily EMA100 and (for G3) 1h ATR% are computed from the same bars without
    look-ahead (indicators update when their bucket closes)."""
    eng = GridEngine(p)
    ema = EMA(p.ema_period)
    atr = ATR(14)
    d1_agg = resample_bucket(bars, Timeframe.D1)
    h1_agg = resample_bucket(bars, Timeframe.H1)
    di = hi = 0
    for b in bars:
        # indicators from buckets that CLOSED before this bar opens
        while di < len(d1_agg) and d1_agg[di].close_time <= b.open_time:
            v = ema.update(float(d1_agg[di].close))
            if start_at is None or b.open_time >= start_at:
                eng.set_trend(
                    b.open_time, d1_agg[di].close, Decimal(str(v)) if v is not None else None
                )
            else:
                eng.s.ema = Decimal(str(v)) if v is not None else None
                eng.s.trend_ok = v is None or float(d1_agg[di].close) > v
            di += 1
        while hi < len(h1_agg) and h1_agg[hi].close_time <= b.open_time:
            a = atr.update(float(h1_agg[hi].high), float(h1_agg[hi].low), float(h1_agg[hi].close))
            if a is not None and p.gap_pct is None and eng.s.anchor != 0:
                eng.set_gap(Decimal(str(a / float(h1_agg[hi].close))) * p.atr_mult)
            hi += 1
        if start_at is not None and b.open_time < start_at:
            continue
        if eng.s.anchor == 0:
            gap_hint = (
                Decimal(str(atr.value / float(b.open))) * p.atr_mult
                if (atr.value and p.gap_pct is None)
                else None
            )
            eng.seed(b.open_time, b.open, gap_hint)
            continue
        eng.on_bar(b.open_time, b.open, b.high, b.low, b.close, 5)
    if eng.s.day_key:
        eng.daily_equity.append((eng.s.day_key, eng.s.equity(eng.s.last_price)))
    return eng


def resample_bucket(bars: list[Candle], tf: Timeframe) -> list[Candle]:
    return resample_1m(bars, tf) if bars else []


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def metrics(eng: GridEngine, p: GridParams, label: str) -> dict[str, Any]:
    daily = eng.daily_equity
    eq0 = float(p.budget_krw)
    eq_end = float(daily[-1][1]) if daily else eq0
    ret = (eq_end / eq0 - 1) * 100
    days = max(1, len(daily))
    cagr = ((eq_end / eq0) ** (365 / days) - 1) * 100 if eq_end > 0 else None
    cyc = eng.cycles
    wins = [c for c in cyc if c.net > 0]
    losses = [c for c in cyc if c.net <= 0]
    gross_w = float(sum((c.net for c in wins), Decimal(0)))
    gross_l = -float(sum((c.net for c in losses), Decimal(0)))
    # underwater & inventory stats from daily curve + events
    hwm, uw, uw_max, min_cash = eq0, 0, 0, float(p.budget_krw)
    inv_vals: list[float] = []
    for _, e in daily:
        ev = float(e)
        hwm = max(hwm, ev)
        uw = uw + 1 if ev < hwm else 0
        uw_max = max(uw_max, uw)
    for evt in eng.events:
        if evt.kind in ("BUY", "SEED", "SELL"):
            min_cash = min(min_cash, float(evt.detail.get("cash", min_cash)))
    inv_series = []
    inv = 0.0
    for evt in eng.events:
        if evt.kind in ("BUY", "SEED"):
            inv += float(evt.detail["fill"]) * float(evt.detail["qty"])
        elif evt.kind == "SELL":
            inv -= float(evt.detail["fill"]) * float(evt.detail["qty"]) - float(evt.detail["net"])
        inv_series.append(max(0.0, inv))
    inv_vals = inv_series or [0.0]
    daily_trades = list(eng._daily_trades.values())
    day_pnls = [float(b[1] - a[1]) for a, b in pairwise(daily)]
    gross_profit_total = float(sum((c.gross for c in cyc), Decimal(0)))
    # Profit factor on TOTAL EQUITY (daily changes incl. ETH mark-to-market) — grid cycles alone
    # never lose by construction, so a cycle-based PF would be meaningless (reported separately).
    up = sum(x for x in day_pnls if x > 0)
    down = -sum(x for x in day_pnls if x < 0)
    pf_equity = (up / down) if down > 0 else (float("inf") if up > 0 else 0.0)
    return {
        "label": label,
        "start": daily[0][0] if daily else None,
        "end": daily[-1][0] if daily else None,
        "days": days,
        "total_return_pct": ret,
        "cagr_pct": cagr,
        "max_drawdown_pct": float(eng.s.mdd * 100),
        "profit_factor": pf_equity,
        "cycle_profit_factor": (gross_w / gross_l)
        if gross_l > 0
        else (float("inf") if gross_w > 0 else 0.0),
        "positive_days_ratio": (len([x for x in day_pnls if x > 0]) / len(day_pnls))
        if day_pnls
        else 0.0,
        "completed_cycles": len(cyc),
        "trades": eng.s.buys + eng.s.sells,
        "win_rate": (len(wins) / len(cyc)) if cyc else 0.0,
        "expectancy_per_cycle": (float(sum((c.net for c in cyc), Decimal(0))) / len(cyc))
        if cyc
        else 0.0,
        "avg_cycle_profit": (float(sum((c.net for c in cyc), Decimal(0))) / len(cyc))
        if cyc
        else 0.0,
        "avg_cycle_duration_h": (sum(c.duration_sec for c in cyc) / len(cyc) / 3600)
        if cyc
        else None,
        "fees": float(eng.s.fees),
        "fee_to_gross_ratio": (float(eng.s.fees) / gross_profit_total)
        if gross_profit_total > 0
        else None,
        "realized": float(eng.s.realized),
        "unrealized_end": float(
            eng.s.inventory_value(eng.s.last_price) - sum((u.cost for u in eng.s.units), Decimal(0))
        ),
        "max_inventory_krw": max(inv_vals),
        "avg_inventory_krw": sum(inv_vals) / len(inv_vals),
        "longest_underwater_days": uw_max,
        "min_cash_krw": min_cash,
        "cash_reserve_violations": sum(
            1
            for e in eng.events
            if e.kind in ("BUY",) and float(e.detail.get("cash", 1e12)) < float(p.min_cash_krw)
        ),
        "inventory_violations": sum(1 for v in inv_vals if v > float(p.max_inventory_krw) * 1.03),
        "daily_trades_mean": (sum(daily_trades) / len(daily_trades)) if daily_trades else 0.0,
        "daily_trades_median": _pct([float(x) for x in daily_trades], 0.5) or 0.0,
        "best_day_krw": max(day_pnls) if day_pnls else 0.0,
        "worst_day_krw": min(day_pnls) if day_pnls else 0.0,
        "halted": eng.s.halted,
        "halt_reason": eng.s.halt_reason,
        "recenters": eng.s.recenters,
        "guard_days_off": sum(1 for e in eng.events if e.kind == "GUARD_ON"),
        "daily_pauses": sum(1 for e in eng.events if e.kind == "PAUSE"),
        "end_equity": eq_end,
    }


def regimes(bars_1d: list[Candle]) -> list[dict[str, Any]]:
    """Label non-overlapping 90-day windows: CRASH (window drawdown ≥ 30%), BEAR (return ≤ −20%),
    BULL (return ≥ +30%), HIGH_VOL (realised daily vol top quartile), else SIDEWAYS."""
    out: list[dict[str, Any]] = []
    closes = [float(c.close) for c in bars_1d]
    import math

    rets = [math.log(b / a) for a, b in pairwise(closes)]
    vols = []
    for i in range(0, len(bars_1d) - 90, 90):
        w = rets[i : i + 89]
        vols.append((sum(x * x for x in w) / len(w)) ** 0.5 if w else 0.0)
    hv = _pct(vols, 0.75) or 0.0
    for i in range(0, len(bars_1d) - 90, 90):
        seg = closes[i : i + 90]
        r = seg[-1] / seg[0] - 1
        peak, dd = seg[0], 0.0
        for x in seg:
            peak = max(peak, x)
            dd = max(dd, (peak - x) / peak)
        w = rets[i : i + 89]
        vol = (sum(x * x for x in w) / len(w)) ** 0.5 if w else 0.0
        label = (
            "CRASH"
            if dd >= 0.30
            else "BEAR"
            if r <= -0.20
            else "BULL"
            if r >= 0.30
            else "HIGH_VOL"
            if vol >= hv
            else "SIDEWAYS"
        )
        out.append(
            {
                "start": bars_1d[i].open_time.date().isoformat(),
                "end": bars_1d[min(i + 89, len(bars_1d) - 1)].open_time.date().isoformat(),
                "label": label,
                "eth_return_pct": r * 100,
                "eth_dd_pct": dd * 100,
            }
        )
    return out


def regime_breakdown(eng: GridEngine, regs: list[dict[str, Any]]) -> dict[str, Any]:
    daily = {d: float(e) for d, e in eng.daily_equity}
    days = sorted(daily)
    cyc_by_day: dict[str, list[float]] = {}
    for c in eng.cycles:
        cyc_by_day.setdefault(c.exit_time.astimezone(KST).strftime("%Y-%m-%d"), []).append(
            float(c.net)
        )
    agg: dict[str, dict[str, Any]] = {}
    for r in regs:
        seg = [d for d in days if r["start"] <= d <= r["end"]]
        if len(seg) < 2:
            continue
        e0, e1 = daily[seg[0]], daily[seg[-1]]
        peak, dd = e0, 0.0
        for d in seg:
            peak = max(peak, daily[d])
            dd = max(dd, (peak - daily[d]) / peak)
        a = agg.setdefault(
            r["label"],
            {
                "windows": 0,
                "return_pct_sum": 0.0,
                "worst_mdd_pct": 0.0,
                "cycles": 0,
                "fees_proxy": 0.0,
                "min_return_pct": 1e9,
                "eth_return_sum": 0.0,
            },
        )
        a["windows"] += 1
        a["return_pct_sum"] += (e1 / e0 - 1) * 100
        a["min_return_pct"] = min(a["min_return_pct"], (e1 / e0 - 1) * 100)
        a["worst_mdd_pct"] = max(a["worst_mdd_pct"], dd * 100)
        a["cycles"] += sum(len(cyc_by_day.get(d, [])) for d in seg)
        a["eth_return_sum"] += r["eth_return_pct"]
    for a in agg.values():
        a["avg_return_pct"] = a["return_pct_sum"] / a["windows"]
        a["avg_eth_return_pct"] = a["eth_return_sum"] / a["windows"]
    return agg


def evaluate_candidate(
    p: GridParams,
    bars: list[Candle],
    *,
    recent_2y_start: datetime,
    recent_6m_start: datetime,
    cost_mults: tuple[float, ...] = (1.5, 2.0),
) -> dict[str, Any]:
    full = run_grid(p, bars)
    bars_1d = resample_bucket(bars, Timeframe.D1)
    regs = regimes(bars_1d)
    out: dict[str, Any] = {
        "params": p.to_dict(),
        "full": metrics(full, p, "full"),
        "regimes": regime_breakdown(full, regs),
        "regime_windows": regs,
        "recent_2y": metrics(
            run_grid(
                p,
                [b for b in bars if b.open_time >= recent_2y_start - timedelta(days=120)],
                start_at=recent_2y_start,
            ),
            p,
            "recent_2y",
        ),
        "recent_6m": metrics(
            run_grid(
                p,
                [b for b in bars if b.open_time >= recent_6m_start - timedelta(days=120)],
                start_at=recent_6m_start,
            ),
            p,
            "recent_6m",
        ),
        "cost_stress": {},
        "daily_equity": [(d, float(e)) for d, e in full.daily_equity],
    }
    for m in cost_mults:
        pm = replace(p, fee_rate=p.fee_rate * Decimal(str(m)), slip=p.slip * Decimal(str(m)))
        out["cost_stress"][f"{m}x"] = metrics(run_grid(pm, bars), pm, f"cost_{m}x")
    return out


def gate(res: dict[str, Any], p: GridParams) -> tuple[bool, dict[str, bool], list[str]]:
    f, r2, r6, cs2 = res["full"], res["recent_2y"], res["recent_6m"], res["cost_stress"]["2.0x"]
    regs = res["regimes"]
    bear_ok = all(
        regs[k]["min_return_pct"] > -float(p.mdd_halt_pct) * 100
        for k in ("BEAR", "CRASH")
        if k in regs
    )
    checks = {
        "total_return_gt_0": f["total_return_pct"] > 0,
        "recent_2y_gt_0": r2["total_return_pct"] > 0,
        "recent_6m_gt_0": r6["total_return_pct"] > 0,
        "pf_ge_1.3": f["profit_factor"] >= 1.3,
        "mdd_le_12": f["max_drawdown_pct"] <= 12.0,
        "cost_2x_gt_0": cs2["total_return_pct"] > 0,
        "fee_ratio_le_0.5": (
            f["fee_to_gross_ratio"] is not None and f["fee_to_gross_ratio"] <= 0.5
        ),
        "bear_crash_survived": bear_ok and not f["halted"],
        "max_inventory_within_policy": f["inventory_violations"] == 0,
        "cash_reserve_violations_0": f["cash_reserve_violations"] == 0,
    }
    failed = [k for k, v in checks.items() if not v]
    return not failed, checks, failed


def combine_portfolio(
    d0_daily: list[tuple[str, float]],
    grid_daily: list[tuple[str, float]],
    d0_only_daily: list[tuple[str, float]],
) -> dict[str, Any]:
    """Daily curves (KST day, equity). D0 sleeve 50k + grid 50k vs D0-only 100k."""
    g = dict(grid_daily)
    d = dict(d0_daily)
    days = sorted(set(g) & set(d))
    if len(days) < 30:
        return {"available": False}
    comb = [(day, d[day] + g[day]) for day in days]
    base = dict(d0_only_daily)
    bdays = [x for x in days if x in base]

    def stats(curve: list[tuple[str, float]]) -> dict[str, Any]:
        eq = [e for _, e in curve]
        peak, mdd = eq[0], 0.0
        for e in eq:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak)
        rets = [b / a - 1 for a, b in pairwise(eq)]
        weekly = [eq[min(i + 7, len(eq) - 1)] / eq[i] - 1 for i in range(0, len(eq) - 7, 7)]
        monthly = [eq[min(i + 30, len(eq) - 1)] / eq[i] - 1 for i in range(0, len(eq) - 30, 30)]
        return {
            "start": curve[0][0],
            "end": curve[-1][0],
            "return_pct": (eq[-1] / eq[0] - 1) * 100,
            "mdd_pct": mdd * 100,
            "weekly_positive_ratio": (sum(1 for w in weekly if w > 0) / len(weekly))
            if weekly
            else None,
            "monthly_positive_ratio": (sum(1 for m in monthly if m > 0) / len(monthly))
            if monthly
            else None,
            "days": len(eq),
            "neg_days": sum(1 for r in rets if r < 0),
        }

    # simultaneous drawdown: days where both sleeves are below their own HWM by > 2%
    pk_d = pk_g = -1.0
    both = 0
    for day in days:
        pk_d, pk_g = max(pk_d, d[day]), max(pk_g, g[day])
        if d[day] < pk_d * 0.98 and g[day] < pk_g * 0.98:
            both += 1
    return {
        "available": True,
        "combined": stats(comb),
        "d0_sleeve_50k": stats([(x, d[x]) for x in days]),
        "grid_sleeve_50k": stats([(x, g[x]) for x in days]),
        "d0_only_100k": stats([(x, base[x]) for x in bdays]) if len(bdays) >= 30 else None,
        "simultaneous_drawdown_days": both,
        "capital_utilization_note": "D0 sleeve: 30% of 50k deployed only while in a position; grid sleeve: inventory 25k-40k deployed continuously",
    }
