"""Round 2: D (4h) / E (1d) trend following on the long 1h history -> docs/research/round2_results.md.

usage: uv run python ../scripts/research_round2.py [--train-days 540] [--test-days 180] [--warmup-days 240] [--only D]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from app.backtest.engine import BacktestConfig
from app.backtest.research import ApprovalCriteria
from app.backtest.research2 import evaluate_round2, format_round2, round2_specs
from app.config.settings import Settings
from app.core.logging import configure_logging
from app.core.types import Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.market.historical import validate_series
from app.risk.position_sizer import SizingParams


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-days", type=int, default=540)
    ap.add_argument("--test-days", type=int, default=180)
    ap.add_argument("--warmup-days", type=int, default=240)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    settings = Settings.load()
    configure_logging("WARNING", json_output=False)
    cfg = settings.config
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    h1 = await repo.load_candles(cfg.system.market, Timeframe.H1)
    v = validate_series(h1)
    dataset = {
        "source": "Upbit REST GET /v1/candles/minutes/60 (unit=60), UTC-hour buckets",
        "timeframe": "1h",
        "first": v.first.isoformat() if v.first else None,
        "last": v.last.isoformat() if v.last else None,
        "bars": v.count,
        "expected_bars": v.expected_minutes,
        "missing_bars": v.missing_minutes,
        "coverage_pct": round(v.coverage_pct, 3),
        "duplicates": v.duplicates,
        "ohlc_violations": v.ohlc_violations,
        "largest_gaps": [
            (a.isoformat(), b.isoformat(), n) for a, b, n in sorted(v.gaps, key=lambda g: -g[2])[:5]
        ],
    }
    mode_risk = cfg.risk.for_mode(TradingMode.BACKTEST)
    bt_cfg = BacktestConfig(
        initial_equity=cfg.challenge.initial_capital_krw,
        fill=FillParams(fee_rate=cfg.risk.fee_rate, slippage_bps=cfg.risk.slippage_bps),
        sizing=SizingParams(
            mode_risk.max_position_pct,
            mode_risk.max_order_krw,
            cfg.risk.min_order_krw,
            cfg.risk.fee_rate,
            cfg.risk.minimum_krw_balance,
        ),
    )
    specs = round2_specs()
    if args.only:
        specs = [s for s in specs if s.key in set(args.only.split(","))]
    out = Path("../docs/research")
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    for spec in specs:
        t0 = datetime.now(UTC)
        print(f"[{spec.key}] {spec.name} ... ", end="", flush=True)
        rep = evaluate_round2(
            spec,
            h1,
            bt_cfg,
            train_days=args.train_days,
            test_days=args.test_days,
            warmup_days=args.warmup_days,
            criteria=ApprovalCriteria(),
            dataset=dataset,
        )
        reports.append(rep)
        print(
            f"{(datetime.now(UTC) - t0).total_seconds():.0f}s -> OOS {rep.oos.net_return_pct:+.2f}% PF {rep.oos.profit_factor:.2f} MDD {rep.oos.max_drawdown_pct:.2f}% trades {rep.oos.trades} -> {rep.verdict}"
        )
        (out / f"round2_{spec.key}.json").write_text(
            json.dumps(rep.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        await repo.add_backtest_run(
            strategy=spec.name,
            params={"grid": spec.grid, "base": spec.base_params},
            timeframe=spec.timeframe,
            start=h1[0].open_time,
            end=h1[-1].open_time,
            metrics={
                "oos_return_pct": rep.oos.net_return_pct,
                "oos_pf": rep.oos.profit_factor,
                "oos_mdd_pct": rep.oos.max_drawdown_pct,
                "verdict": rep.verdict,
            },
            equity_curve_path=str(out / f"round2_{spec.key}.json"),
            is_walk_forward=True,
        )
    header = [
        "# Strategy research round 2 — low-frequency trend following on 1h history",
        "",
        f"- dataset: {dataset['source']}; {dataset['first']} → {dataset['last']}; {dataset['bars']:,} bars, coverage {dataset['coverage_pct']}% (missing {dataset['missing_bars']}, dup {dataset['duplicates']}, ohlc violations {dataset['ohlc_violations']}); largest gaps {dataset['largest_gaps']}",
        "- base bars 1h (intrabar stop at 1h resolution); 4h/1d built by CandleAggregator (UTC buckets); fills at next 1h open; STOP-first rule",
        f"- walk-forward: train {args.train_days}d / test {args.test_days}d rolled by {args.test_days}d, {args.warmup_days}d warm-up prefix (indicator/channel state only — no trades, no equity); selection = train return / max(MDD,1) with >=3 trades",
        f"- costs: fee {cfg.risk.fee_rate}/side, slippage {cfg.risk.slippage_bps} bps; stress 1.5x / 2.0x",
        f"- sizing: {mode_risk.max_position_pct} of equity, cap ₩{mode_risk.max_order_krw:,.0f}, min ₩{cfg.risk.min_order_krw:,.0f}, 1 position",
        "- criteria (unchanged): OOS return>0, PF>=1.20, MDD<=10%, profitable windows>=60%, OOS trades>=50 (30..49 -> REVIEW only), neighbour stability, 1.5x cost, no concentration risk",
        "",
    ]
    md = "\n".join(header) + format_round2(reports)
    (out / "round2_results.md").write_text(md, encoding="utf-8")
    print(md)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
