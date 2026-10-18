"""D0 exit-rule research E0..E4 -> docs/research/exit_round_{E}.json + exit_round_results.md.
usage: uv run python ../scripts/research_exit_round.py [--workers 5] [--only E0,E1]
Read-only on data/quant.db (1h candles); nothing in production is written."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import yaml

from app.core.types import Timeframe
from app.research.exit_round.engine import exit_rules
from app.research.exit_round.harness import classify, evaluate_rule, match_trades
from app.research.round3.market_reader import MarketReader
from app.research.round3.runner import backtest_config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "research"
START = datetime(2017, 9, 25, tzinfo=UTC)
END = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)  # same last bar as the frozen 9Y D0 run
R2Y = datetime(2024, 9, 25, tzinfo=UTC)
YTD = datetime(2026, 1, 1, tzinfo=UTC)


def _params() -> dict[str, float]:
    y = yaml.safe_load((ROOT / "config/strategies/trend_following_4h_v1.yaml").read_text())
    return {k: float(v) for k, v in y["params"].items()}


def _run(key: str) -> dict[str, Any]:
    rule = next(r for r in exit_rules() if r.key == key)
    reader = MarketReader(ROOT / "data" / "quant.db", "KRW-BTC")
    candles = reader.load(Timeframe.H1, START, END)
    reader.close()
    t0 = time.monotonic()
    res = evaluate_rule(
        rule,
        _params(),
        candles,
        backtest_config(100_000),
        start=START,
        end=END,
        train_days=540,
        test_days=180,
        warmup_days=240,
        recent_2y_start=R2Y,
        ytd_start=YTD,
    )
    res["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return res


def _p(x: dict[str, Any]) -> str:
    return f"{x['net_return_pct']:+.1f}% / {x['max_drawdown_pct']:.2f}% / {x['profit_factor']:.2f} / {x['trades']}"


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(
        json.dumps({k: v for k, v in d.items() if k != "elapsed_sec"}, default=str)
    )
    return out


def fmt(
    results: dict[str, dict[str, Any]],
    matches: dict[str, dict[str, Any]],
    verdicts: dict[str, tuple[str, list[str]]],
) -> str:
    ids = [k for k in ("E0", "E1", "E2", "E3", "E4") if k in results]
    lines: list[str] = ["# D0 exit-rule research — E0 control vs E1..E4 (generated)", ""]
    r0 = results[ids[0]]
    lines += [
        f"- dataset 1h {r0['dataset']['first']} → {r0['dataset']['last']} ({r0['dataset']['bars_1h']:,} bars); WF 540/180/180/240 fixed params, {r0['wf_setup']['windows']} windows; entries = D0 (TrendFollowing4h, frozen params); only the exit stack differs",
        "",
    ]
    for k in ids:
        lines.append(f"- **{k}** — {results[k]['rule']['description']}")
    lines += [
        "",
        "## Walk-forward OOS",
        "",
        "| id | return | MDD | PF | trades | win | expectancy | avg win | avg loss | payoff | fees | exposure | prof/lose | best | worst |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ids:
        o = results[k]["oos"]
        f = results[k]["full_period"]
        lines.append(
            f"| {k} | {o['net_return_pct']:+.1f}% | {o['max_drawdown_pct']:.2f}% | {o['profit_factor']:.2f} | {o['trades']} | {o['win_rate'] * 100:.1f}% | ₩{o['expectancy']:,.0f} | ₩{f['average_win']:,.0f} | ₩{f['average_loss']:,.0f} | {f['payoff_ratio']:.2f} | ₩{o['fees']:,.0f} | {o['exposure_pct']:.1f}% | {o['profitable_windows']}/{o['losing_windows']} | {o['best_window']['test_return_pct']:+.1f}% | {o['worst_window']['test_return_pct']:+.1f}% |"
        )
    lines += [
        "",
        "## Full period / Recent 2Y / 2026 YTD / cost stress / concentration",
        "",
        "| id | Full 9Y ret/MDD/PF/n (CAGR) | Recent 2Y | 2026 YTD | 1.5x | 2.0x | top1 | top5 | ex-top5 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ids:
        r = results[k]
        f, y, t, cs, c = (
            r["full_period"],
            r["recent_2y"],
            r["ytd_2026"],
            r["cost_stress"],
            r["concentration"],
        )
        lines.append(
            f"| {k} | {_p(f)} ({f['cagr_pct']:.1f}%) | {_p(y)} | {_p(t)} | {cs['1.5x']['oos_return_pct']:+.1f}% | {cs['2.0x']['oos_return_pct']:+.1f}% | {c['top1_contribution']:.0%} | {c['top5_contribution']:.0%} | {c['return_pct_ex_top5']:+.1f}% |"
        )
    lines += [
        "",
        "## Profit-to-loss reversals & giveback (full period, 116-ish positions)",
        "",
        "| id | trades | losses | P→L (MFE>0) | meaningful (MFE≥0.5R) | ≥1R→loss | ≥2R→loss | reached 1R / 2R | giveback R mean/median/p90 | giveback after 1R mean/p90 | hold mean / median | avg MFE R | avg realized R |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ids:
        v = results[k]["full_period"]["reversal"]
        g = v["giveback_r"]
        g1 = v["giveback_r_after_1R"]
        h = v["holding_minutes"]
        lines.append(
            f"| {k} | {v['trades']} | {v['losing_trades']} | {v['profit_to_loss']['count']} ({v['profit_to_loss']['ratio']:.0%}) | {v['meaningful_profit_to_loss_0_5R']['count']} ({v['meaningful_profit_to_loss_0_5R']['ratio']:.0%}) | {v['reached_1R_then_loss']['count']} | {v['reached_2R_then_loss']['count']} | {v['reached_1R']} / {v['reached_2R']} | {g['mean']:.2f} / {g['median']:.2f} / {g['p90']:.2f} | {g1['mean']:.2f} / {g1['p90']:.2f} | {h['mean'] / 60:.0f}h / {h['median'] / 60:.0f}h | {v['avg_mfe_r']:.2f} | {v['avg_realized_r']:.2f} |"
        )
    lines += [
        "",
        "## Trade-matched vs E0 (full period, identical entries)",
        "",
        "| id | matched | E0-only | cand-only | matched PnL E0 → cand | upside capture | winner capture | earlier exits | top-10 E0 winners ΔPnL (cut early) | mean hold E0 → cand |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ids[1:]:
        m = matches[k]
        lines.append(
            f"| {k} | {m['matched']} | {m['e0_only']} | {m['cand_only']} | ₩{m['matched_e0_pnl']:,.0f} → ₩{m['matched_cand_pnl']:,.0f} | {m['upside_capture_ratio']:.2f} | {m['winner_capture_ratio']:.2f} | {m['earlier_exits']} | ₩{m['top10_pnl_diff_total']:,.0f} ({m['top10_cut_early']}/10) | {m['mean_hold_min_e0'] / 60:.0f}h → {m['mean_hold_min_cand'] / 60:.0f}h |"
        )
    lines += ["", "## Verdicts (pre-registered reading of the §15 criteria)", ""]
    for k in ids[1:]:
        v, notes = verdicts[k]
        lines.append(
            f"- **{k}: {v}**" + (f" — {'; '.join(notes)}" if notes else " — all checks met")
        )
    lines += ["", "## Representative trades (largest |ΔPnL| vs E0)", ""]
    for k in ids[1:]:
        lines += [
            f"### {k}",
            "",
            "| entry | R% | E0 exit / reason / PnL / hold | cand exit / reason / PnL / hold | ΔPnL | E0 MFE R |",
            "|---|---|---|---|---|---|",
        ]
        for r in matches[k]["representative"]:
            lines.append(
                f"| {r['entry_time'][:16]} | {r['r_pct']:.2f}% | {str(r['e0_exit_time'])[:16]} / {r['e0_exit_reason']} / ₩{r['e0_pnl']:,.0f} / {(r['e0_hold_min'] or 0) / 60:.0f}h | {str(r['c_exit_time'])[:16]} / {r['c_exit_reason']} / ₩{r['c_pnl']:,.0f} / {(r['c_hold_min'] or 0) / 60:.0f}h | ₩{r['pnl_diff']:+,.0f} | {r['e0_mfe_r']:.2f} |"
            )
        lines.append("")
    lines += [
        "## Per-window OOS returns (%)",
        "",
        "| test_end | " + " | ".join(ids) + " | B&H |",
        "|---|" + "---|" * (len(ids) + 1),
    ]
    for i, w in enumerate(r0["windows"]):
        lines.append(
            f"| {w['test_end'][:10]} | "
            + " | ".join(f"{results[k]['windows'][i]['test_return_pct']:+.1f}" for k in ids)
            + f" | {w['test_bh_return_pct']:+.1f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    ids = [r.key for r in exit_rules()]
    if args.only:
        ids = [i for i in ids if i in set(args.only.split(","))]
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = dict(zip(ids, ex.map(_run, ids), strict=True))
    print(
        f"parallel run {time.monotonic() - t0:.0f}s: "
        + ", ".join(f"{k} {v['elapsed_sec']}s" for k, v in results.items())
    )
    serial = _run("E0")
    results["E0"]["determinism_check"] = {"identical": _strip(serial) == _strip(results["E0"])}
    print("determinism E0:", results["E0"]["determinism_check"])
    matches = {k: match_trades(results["E0"], results[k]) for k in ids if k != "E0"}
    verdicts = {k: classify(results["E0"], results[k], matches[k]) for k in ids if k != "E0"}
    for k in ids:
        if k != "E0":
            results[k]["match_vs_e0"] = matches[k]
            results[k]["verdict"] = {"verdict": verdicts[k][0], "notes": verdicts[k][1]}
        (OUT / f"exit_round_{k}.json").write_text(
            json.dumps(results[k], indent=1, default=str), encoding="utf-8"
        )
    md = fmt(results, matches, verdicts)
    (OUT / "exit_round_results.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
