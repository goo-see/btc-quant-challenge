"""ETH grid fast-track research: G0..G3 on the full KRW-ETH 5m history (data/eth_market.db, read-only)
+ D0+grid portfolio combination -> docs/research/eth_grid_{G}.json, eth_grid_results.md.
usage: uv run python ../scripts/research_eth_grid.py [--workers 4] [--only G0,G1]"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import yaml

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.core.types import Timeframe
from app.execution.fill_model import FillParams
from app.research.eth_grid.engine import KST, grid_candidates
from app.research.eth_grid.harness import combine_portfolio, evaluate_candidate, gate
from app.research.round3.market_reader import MarketReader
from app.risk.position_sizer import SizingParams
from app.strategy.trend_following import TrendFollowing4h

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "research"
ETH_DB = ROOT / "data" / "eth_market.db"
BTC_DB = ROOT / "data" / "quant.db"


def _bars() -> list[Any]:
    r = MarketReader(ETH_DB, "KRW-ETH")
    bars = r.load(Timeframe.M5)
    r.close()
    return bars


def _run(key: str) -> dict[str, Any]:
    p = next(c for c in grid_candidates() if c.key == key)
    bars = _bars()
    end = bars[-1].open_time
    t0 = time.monotonic()
    res = evaluate_candidate(
        p,
        bars,
        recent_2y_start=end - timedelta(days=730),
        recent_6m_start=end - timedelta(days=182),
    )
    ok, checks, failed = gate(res, p)
    res["gate"] = {"pass": ok, "checks": checks, "failed": failed}
    res["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return res


def _d0_daily(
    initial: int, max_order: int, start: datetime, end: datetime
) -> list[tuple[str, float]]:
    """Daily (KST) equity of the frozen D0 on BTC 1h, sized for the given sleeve."""
    params = {
        k: float(v)
        for k, v in yaml.safe_load(
            (ROOT / "config/strategies/trend_following_4h_v1.yaml").read_text()
        )["params"].items()
    }
    r = MarketReader(BTC_DB, "KRW-BTC")
    h1 = r.load(Timeframe.H1, start - timedelta(days=240), end)
    r.close()
    cfg = BacktestConfig(
        Decimal(initial),
        FillParams(Decimal("0.0005"), Decimal(3)),
        SizingParams(
            Decimal("0.30"), Decimal(max_order), Decimal(5000), Decimal("0.0005"), Decimal(1000)
        ),
    )
    res = BacktestEngine(TrendFollowing4h(Timeframe.H4, Timeframe.D1, params), cfg).run(
        h1, start_at=start
    )
    daily: dict[str, float] = {}
    for ts, eq in res.equity_curve:
        daily[ts.astimezone(KST).strftime("%Y-%m-%d")] = float(eq)
    return sorted(daily.items())


def fmt(results: dict[str, dict[str, Any]], port: dict[str, Any], selected: str) -> str:
    ids = [k for k in ("G0", "G1", "G2", "G3") if k in results]
    f0 = results[ids[0]]["full"]
    lines: list[str] = [
        "# ETH/KRW micro-grid fast-track research — G0..G3 (generated)",
        "",
        f"- dataset KRW-ETH 5m {f0['start']} → {f0['end']} ({f0['days']} days); sleeve ₩50,000, unit ₩5,000, seed ₩25,000, max inventory ₩40,000, min cash ₩10,000; fee 0.05%/side (orders/chance), slip+half-spread 5 bps/side; daily EMA100 buy guard; daily loss −5% pause; MDD 12% halt; pessimistic bar feed (no same-bar buy→sell)",
        "",
        "## Total sleeve equity (cash + ETH mark-to-market − fees)",
        "",
        "| id | gap | return | CAGR | MDD | PF | cycles | trades | win | exp/cycle | avg cycle h | fees | fee/gross | max inv | avg inv | underwater d | min cash | trades/day mean/med | best/worst day | halted | recenters |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ids:
        r = results[k]
        f = r["full"]
        lines.append(
            f"| {k} | {r['params']['description']} | {f['total_return_pct']:+.2f}% | {f['cagr_pct']:+.2f}% | {f['max_drawdown_pct']:.2f}% | {f['profit_factor']:.2f} | {f['completed_cycles']} | {f['trades']} | {f['win_rate'] * 100:.0f}% | ₩{f['expectancy_per_cycle']:,.0f} | {f['avg_cycle_duration_h'] or 0:.1f} | ₩{f['fees']:,.0f} | {f['fee_to_gross_ratio'] if f['fee_to_gross_ratio'] is None else f'{f["fee_to_gross_ratio"]:.2f}'} | ₩{f['max_inventory_krw']:,.0f} | ₩{f['avg_inventory_krw']:,.0f} | {f['longest_underwater_days']} | ₩{f['min_cash_krw']:,.0f} | {f['daily_trades_mean']:.1f}/{f['daily_trades_median']:.0f} | ₩{f['best_day_krw']:,.0f}/₩{f['worst_day_krw']:,.0f} | {f['halted']} | {f['recenters']} |"
        )
    lines += [
        "",
        "## Recent windows & cost stress",
        "",
        "| id | Recent 2Y ret/MDD/PF/cycles | Recent 6M ret/MDD/PF/cycles | 1.5x cost ret/PF | 2.0x cost ret/PF |",
        "|---|---|---|---|---|",
    ]
    for k in ids:
        r = results[k]
        a, b, c1, c2 = (
            r["recent_2y"],
            r["recent_6m"],
            r["cost_stress"]["1.5x"],
            r["cost_stress"]["2.0x"],
        )
        lines.append(
            f"| {k} | {a['total_return_pct']:+.2f}% / {a['max_drawdown_pct']:.2f}% / {a['profit_factor']:.2f} / {a['completed_cycles']} | {b['total_return_pct']:+.2f}% / {b['max_drawdown_pct']:.2f}% / {b['profit_factor']:.2f} / {b['completed_cycles']} | {c1['total_return_pct']:+.2f}% / {c1['profit_factor']:.2f} | {c2['total_return_pct']:+.2f}% / {c2['profit_factor']:.2f} |"
        )
    lines += [
        "",
        "## Regimes (90-day windows; avg sleeve return / worst window / worst MDD / cycles)",
        "",
    ]
    labels = ["BULL", "SIDEWAYS", "HIGH_VOL", "BEAR", "CRASH"]
    lines += ["| id | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for k in ids:
        rg = results[k]["regimes"]
        lines.append(
            f"| {k} | "
            + " | ".join(
                (
                    f"{rg[lab]['windows']}w avg {rg[lab]['avg_return_pct']:+.1f}% min {rg[lab]['min_return_pct']:+.1f}% mdd {rg[lab]['worst_mdd_pct']:.1f}% cyc {rg[lab]['cycles']}"
                    if lab in rg
                    else "—"
                )
                for lab in labels
            )
            + " |"
        )
    lines += ["", "## Pre-registered gate", ""]
    for k in ids:
        g = results[k]["gate"]
        lines.append(
            f"- **{k}: {'PASS' if g['pass'] else 'FAIL'}** — failed: {g['failed'] or 'none'}"
        )
    lines += [
        "",
        f"**SELECTED_GRID = {selected}**",
        "",
        "## Portfolio: D0 only (100k) vs BTC D0 50k + ETH grid 50k",
        "",
    ]
    if port.get("available"):
        for name in ("d0_only_100k", "d0_sleeve_50k", "grid_sleeve_50k", "combined"):
            s = port.get(name)
            if s:
                lines.append(
                    f"- **{name}**: {s['start']} → {s['end']} return {s['return_pct']:+.2f}% · MDD {s['mdd_pct']:.2f}% · weekly+ {s['weekly_positive_ratio']:.0%} · monthly+ {s['monthly_positive_ratio']:.0%}"
                )
        lines.append(
            f"- simultaneous drawdown days (both sleeves > 2% below HWM): {port['simultaneous_drawdown_days']}; {port['capital_utilization_note']}"
        )
    else:
        lines.append("- portfolio combination not available (insufficient overlap)")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    ids = [c.key for c in grid_candidates()]
    if args.only:
        ids = [i for i in ids if i in set(args.only.split(","))]
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = dict(zip(ids, ex.map(_run, ids), strict=True))
    print(
        f"parallel {time.monotonic() - t0:.0f}s: "
        + ", ".join(f"{k} {v['elapsed_sec']}s" for k, v in results.items())
    )
    # selection: among gate passers, rank by (PF, -MDD, return, 2x cost return, recent 6M) — never return alone
    passers = [k for k in ids if results[k]["gate"]["pass"]]

    def score(k: str) -> float:
        f, c2, r6 = results[k]["full"], results[k]["cost_stress"]["2.0x"], results[k]["recent_6m"]
        return (
            min(f["profit_factor"], 5.0) * 10
            - f["max_drawdown_pct"] * 2
            + f["total_return_pct"] / 10
            + c2["total_return_pct"] / 10
            + r6["total_return_pct"] / 5
            - f["max_inventory_krw"] / 10000
        )

    selected = max(passers, key=score) if passers else "NONE"
    port: dict[str, Any] = {"available": False}
    if selected != "NONE":
        gd = results[selected]["daily_equity"]
        start = datetime.fromisoformat(gd[0][0]).replace(tzinfo=KST).astimezone(UTC)
        end = datetime.fromisoformat(gd[-1][0]).replace(tzinfo=KST).astimezone(UTC) + timedelta(
            days=1
        )
        d0_50 = _d0_daily(50000, 15000, start, end)
        d0_100 = _d0_daily(100000, 30000, start, end)
        port = combine_portfolio(d0_50, gd, d0_100)
    OUT.mkdir(parents=True, exist_ok=True)
    for k, r in results.items():
        slim = {kk: vv for kk, vv in r.items() if kk != "daily_equity"}
        (OUT / f"eth_grid_{k}.json").write_text(
            json.dumps(slim, indent=1, default=str), encoding="utf-8"
        )
    (OUT / "eth_grid_portfolio.json").write_text(
        json.dumps({"selected": selected, "portfolio": port}, indent=1, default=str),
        encoding="utf-8",
    )
    md = fmt(results, port, selected)
    (OUT / "eth_grid_results.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
