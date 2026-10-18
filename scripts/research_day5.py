"""DAY 5 strategy research: A/B/C under identical conditions -> docs/research/day5_results.md + json.

usage: uv run python ../scripts/research_day5.py [--train-days 180] [--test-days 60] [--base 15m] [--only A,B]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from app.backtest.engine import BacktestConfig
from app.backtest.research import ApprovalCriteria, default_specs, evaluate_strategy, format_report
from app.config.settings import Settings
from app.core.logging import configure_logging
from app.core.types import Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=60)
    ap.add_argument("--base", default="15m", choices=["5m", "15m"])
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    settings = Settings.load()
    configure_logging("WARNING", json_output=False)
    cfg = settings.config
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    m1 = await repo.load_candles(cfg.system.market, Timeframe.M1)
    base = resample_1m(m1, Timeframe(args.base))
    mode_risk = cfg.risk.for_mode(TradingMode.BACKTEST)  # identical LIVE-fidelity sizing for all
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
    specs = default_specs()
    if args.only:
        keep = set(args.only.split(","))
        specs = [s for s in specs if s.key in keep]
    out_dir = Path("../docs/research")
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for spec in specs:
        t0 = datetime.now(UTC)
        print(f"[{spec.key}] {spec.name} ... ", end="", flush=True)
        rep = evaluate_strategy(
            spec,
            base,
            bt_cfg,
            train_days=args.train_days,
            test_days=args.test_days,
            criteria=ApprovalCriteria(),
        )
        reports.append(rep)
        print(
            f"done in {(datetime.now(UTC) - t0).total_seconds():.0f}s -> OOS {rep.oos.net_return_pct:+.2f}% PF {rep.oos.profit_factor:.2f} trades {rep.oos.trades} approved={rep.approved}"
        )
        (out_dir / f"day5_{spec.key}.json").write_text(
            json.dumps(rep.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        await repo.add_backtest_run(
            strategy=spec.name,
            params={"grid": spec.grid, "base": spec.base_params},
            timeframe=spec.timeframe,
            start=base[0].open_time,
            end=base[-1].open_time,
            metrics={
                "oos_return_pct": rep.oos.net_return_pct,
                "oos_pf": rep.oos.profit_factor,
                "oos_mdd_pct": rep.oos.max_drawdown_pct,
                "approved": rep.approved,
            },
            equity_curve_path=str(out_dir / f"day5_{spec.key}.json"),
            is_walk_forward=True,
        )
    header = [
        "# DAY 5 strategy research — identical conditions",
        "",
        f"- dataset: KRW-BTC 1m {m1[0].open_time:%Y-%m-%d} → {m1[-1].open_time:%Y-%m-%d} ({len(m1):,} bars) resampled to {args.base} ({len(base):,} bars)",
        f"- walk-forward: train {args.train_days}d / test {args.test_days}d, rolled by {args.test_days}d; selection score = train return / max(MDD,1) with >=5 trades",
        f"- costs: fee {cfg.risk.fee_rate} per side, slippage {cfg.risk.slippage_bps} bps; stress 1.5x / 2.0x on both",
        f"- sizing: {mode_risk.max_position_pct} of equity, cap ₩{mode_risk.max_order_krw:,.0f}, min ₩{cfg.risk.min_order_krw:,.0f}, 1 position; fills at next 15m open; STOP-first intrabar rule",
        "- criteria: OOS return>0, PF>=1.20, MDD<=10%, profitable windows>=60%, OOS trades>=50, neighbour positive-expectancy>=60% & no cliff, 1.5x costs keep >=50% of return",
        "",
    ]
    md = "\n".join(header) + format_report(reports)
    (out_dir / "day5_results.md").write_text(md, encoding="utf-8")
    print(md)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
