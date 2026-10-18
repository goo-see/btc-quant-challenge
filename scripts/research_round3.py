"""Round 3 historical evaluation D0..D6 (fixed params) -> docs/research/round3_{D}.json + round3_results.md.

usage: uv run python ../scripts/research_round3.py [--workers 7] [--only D0,D1] [--no-determinism-check]
Runs strategies in separate processes (each loads the 1h candles itself, read-only) and merges
the results; the determinism check re-runs D0 serially in the parent and compares the JSON.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.types import Timeframe
from app.research.round3.config import DEFAULT_CONFIG, load_config
from app.research.round3.historical import evaluate_strategy, timing_vs_control
from app.research.round3.market_reader import MarketReader
from app.research.round3.runner import backtest_config

OUT = Path(__file__).resolve().parents[1] / "docs" / "research"


def _run(sid: str) -> dict[str, Any]:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy(sid)
    reader = MarketReader(cfg.market_db, cfg.market)
    candles = reader.load(Timeframe.H1, cfg.historical.start, cfg.historical.end)
    reader.close()
    t0 = time.monotonic()
    res = evaluate_strategy(
        sd, candles, backtest_config(cfg.initial_virtual_equity), cfg.historical, cfg.gate
    )
    res["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return res


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    d = json.loads(json.dumps(d))
    d.pop("elapsed_sec", None)
    return d


def fmt(results: dict[str, dict[str, Any]], cfg: Any) -> str:
    ids = [s.strategy_id for s in cfg.strategies if s.strategy_id in results]
    lines: list[str] = []
    r0 = results[ids[0]]
    lines += [
        f"# {cfg.title} — historical results (generated)",
        "",
        f"- experiment `{cfg.experiment_id}` · dataset 1h {r0['dataset']['first']} → {r0['dataset']['last']} ({r0['dataset']['bars_1h']:,} bars) · WF train {cfg.historical.train_days}d / test {cfg.historical.test_days}d / roll {cfg.historical.roll_days}d / warm-up {cfg.historical.warmup_days}d · FIXED params (no selection) · {r0['wf_setup']['windows']} windows",
        "- costs fee 0.05%/side + slippage 3 bps (stress 1.5x / 2.0x); sizing 30% equity, cap ₩30,000, min ₩5,000, 1 position; next-1h-open fill; STOP-first",
        "",
        "## Walk-forward OOS (stitched 180d test windows, ₩100,000 reset per window)",
        "",
        "| id | TF | horizon | OOS return | OOS MDD | PF | trades | win rate | expectancy | exposure | fees | prof/lose windows | best window | worst window | B&H |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sid in ids:
        r, o, s = results[sid], results[sid]["oos"], results[sid]["strategy"]
        bw, ww = o["best_window"], o["worst_window"]
        lines.append(
            f"| {sid} | {s['timeframe']} | {s['horizon_days']}d | {o['net_return_pct']:+.2f}% | {o['max_drawdown_pct']:.2f}% | {o['profit_factor']:.2f} | {o['trades']} | {o['win_rate'] * 100:.1f}% | ₩{o['expectancy']:,.0f} | {o['exposure_pct']:.1f}% | ₩{o['fees']:,.0f} | {o['profitable_windows']}/{o['losing_windows']} of {o['windows']} | {bw['test_return_pct']:+.1f}% ({bw['test_end'][:7]}) | {ww['test_return_pct']:+.1f}% ({ww['test_end'][:7]}) | {o['buy_hold_return_pct']:+.0f}% |"
        )
    lines += [
        "",
        "## Cost stress (WF OOS) and trade concentration",
        "",
        "| id | 1.0x ret / PF | 1.5x ret / PF | 2.0x ret / PF | top1 | top3 | top5 | ex-top1 | ex-top5 | concentration risk |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sid in ids:
        cs, c = results[sid]["cost_stress"], results[sid]["concentration"]
        lines.append(
            f"| {sid} | {cs['1.0x']['oos_return_pct']:+.1f}% / {cs['1.0x']['oos_pf']:.2f} | {cs['1.5x']['oos_return_pct']:+.1f}% / {cs['1.5x']['oos_pf']:.2f} | {cs['2.0x']['oos_return_pct']:+.1f}% / {cs['2.0x']['oos_pf']:.2f} | {c['top1_contribution']:.0%} | {c['top3_contribution']:.0%} | {c['top5_contribution']:.0%} | {c['return_pct_ex_top1']:+.1f}% | {c['return_pct_ex_top5']:+.1f}% | {'YES' if c['concentration_risk'] else 'no'} |"
        )
    lines += [
        "",
        "## Fixed-parameter periods (single continuous run, 240d warm-up prefix for the sub-periods)",
        "",
        "| id | Full 9Y ret / MDD / PF / trades | Recent 2Y ret / MDD / PF / trades | 2026 YTD ret / MDD / PF / trades | fake breakouts (full) | fake breakouts (OOS) |",
        "|---|---|---|---|---|---|",
    ]
    for sid in ids:
        r = results[sid]
        f, y2, yt = r["full_period"], r["recent_2y"], r["ytd_2026"]
        fb, fo = r["fake_breakout_full"], r["fake_breakout_oos"]

        def p(x: dict[str, Any]) -> str:
            return (
                f"{x['net_return_pct']:+.2f}% / {x['max_drawdown_pct']:.2f}% / {x['profit_factor']:.2f} / {x['trades']}"
                if x.get("available")
                else "n/a"
            )

        lines.append(
            f"| {sid} | {p(f)} | {p(y2)} | {p(yt)} | {fb['count']}/{f.get('trades', 0)} ({fb['rate']:.0%}) | {fo['count']}/{r['oos']['trades']} ({fo['rate']:.0%}) |"
        )
    lines += [
        "",
        "## Entry timing vs D0 (full period, informational)",
        "",
        "| id | D0 entries matched | match rate | earlier than D0 | median lead h | mean lead h | extra entries w/o D0 entry |",
        "|---|---|---|---|---|---|---|",
    ]
    for sid in ids:
        t = results[sid].get("timing_vs_d0")
        if t:
            lines.append(
                f"| {sid} | {t['d0_entries_matched']}/{t['d0_entries']} | {t['match_rate']:.0%} | {t['earlier_than_d0']} | {t['median_lead_hours'] if t['median_lead_hours'] is not None else '—'} | {f'{t["mean_lead_hours"]:.1f}' if t['mean_lead_hours'] is not None else '—'} | {t['other_entries_without_d0_entry']} |"
            )
    lines += ["", "## Pre-registered gate (next-round candidate status only)", ""]
    for sid in ids:
        g = results[sid]["gate"]
        fails = [k for k, v in g["checks"].items() if not v]
        lines.append(
            f"- **{sid}: {g['verdict']}** — failed checks: {fails if fails else 'none'}"
            + (f"; {'; '.join(g['notes'])}" if g["notes"] else "")
        )
    lines += [
        "",
        "## Per-window OOS returns (%)",
        "",
        "| test_end | " + " | ".join(ids) + " | B&H |",
        "|---|" + "---|" * (len(ids) + 1),
    ]
    for i, w in enumerate(r0["windows"]):
        lines.append(
            f"| {w['test_end'][:10]} | "
            + " | ".join(f"{results[s]['windows'][i]['test_return_pct']:+.1f}" for s in ids)
            + f" | {w['test_bh_return_pct']:+.1f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--only", default=None)
    ap.add_argument("--no-determinism-check", action="store_true")
    args = ap.parse_args()
    cfg = load_config(DEFAULT_CONFIG)
    ids = [s.strategy_id for s in cfg.strategies]
    if args.only:
        ids = [i for i in ids if i in set(args.only.split(","))]
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = dict(zip(ids, ex.map(_run, ids), strict=True))
    print(
        f"parallel run {time.monotonic() - t0:.0f}s: "
        + ", ".join(f"{k} {v['elapsed_sec']}s" for k, v in results.items())
    )
    if not args.no_determinism_check and cfg.control in results:
        serial = _run(cfg.control)
        same = _strip(serial) == _strip(results[cfg.control])
        print(
            f"determinism check ({cfg.control} parallel vs serial): {'IDENTICAL' if same else 'MISMATCH'}"
        )
        results[cfg.control]["determinism_check"] = {"identical": same}
    if cfg.control in results:
        for sid in ids:
            if sid != cfg.control:
                results[sid]["timing_vs_d0"] = timing_vs_control(results[cfg.control], results[sid])
    OUT.mkdir(parents=True, exist_ok=True)
    for sid, r in results.items():
        (OUT / f"round3_{sid}.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8"
        )
    md = fmt(results, cfg)
    (OUT / "round3_results.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
