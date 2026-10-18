"""Walk-forward parameter validation on stored candles (fast sweep on 15m base).

usage: uv run python ../scripts/run_walk_forward.py [--train-days 180] [--test-days 60] [--base 15m]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from app.backtest.engine import BacktestConfig
from app.backtest.walk_forward import run_walk_forward
from app.config.settings import Settings
from app.core.logging import configure_logging
from app.core.types import Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams

# 1.5x ATR stops on 15m were dominated by the 0.1% round-trip fee in the 2026 prelim run,
# so the grid explores wider stops/trails instead. 3*3*3*2 = 54 combos.
DEFAULT_GRID: dict[str, list[float]] = {
    "ema_fast": [8, 12, 20],
    "ema_slow": [26, 48, 96],
    "atr_stop_mult": [2.0, 3.0, 4.0],
    "atr_trail_mult": [3.0, 5.0],
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=60)
    ap.add_argument("--base", default="15m", choices=["1m", "5m", "15m"])
    ap.add_argument("--grid", type=str, default=None, help="JSON dict of param -> list")
    args = ap.parse_args()

    settings = Settings.load()
    configure_logging("WARNING", json_output=False)
    cfg = settings.config
    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    candles = await repo.load_candles(cfg.system.market, Timeframe.M1)
    if not candles:
        raise SystemExit("no candles — run fetch_history first")
    base_tf = Timeframe(args.base)
    base = candles if base_tf is Timeframe.M1 else resample_1m(candles, base_tf)

    mode_risk = cfg.risk.for_mode(TradingMode.BACKTEST)
    bt_cfg = BacktestConfig(
        initial_equity=cfg.challenge.initial_capital_krw,
        fill=FillParams(fee_rate=cfg.risk.fee_rate, slippage_bps=cfg.risk.slippage_bps),
        sizing=SizingParams(
            max_position_pct=mode_risk.max_position_pct,
            max_order_krw=mode_risk.max_order_krw,
            min_order_krw=cfg.risk.min_order_krw,
            fee_rate=cfg.risk.fee_rate,
            minimum_krw_balance=cfg.risk.minimum_krw_balance,
        ),
    )
    grid = json.loads(args.grid) if args.grid else DEFAULT_GRID
    started = datetime.now(UTC)
    wf = run_walk_forward(
        candles=base,
        strategy_name=cfg.strategy.name,
        timeframe=cfg.strategy.timeframe,
        trend_timeframe=cfg.strategy.trend_timeframe,
        base_params=dict(cfg.strategy.params),
        grid=grid,
        cfg=bt_cfg,
        train_days=args.train_days,
        test_days=args.test_days,
    )
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(
        f"\n=== WALK-FORWARD grid={wf.grid_size} train={args.train_days}d test={args.test_days}d base={args.base} ({elapsed:.0f}s) ==="
    )
    print(
        f"{'train':<23}{'test_end':<12}{'params':<45}{'trainRet%':>10}{'trainMDD%':>10}{'testRet%':>9}{'testMDD%':>9}{'tTrades':>8}"
    )
    for w in wf.windows:
        tm, sm = w.train_metrics, w.test_metrics
        print(
            f"{w.train_start:%Y-%m-%d}->{w.train_end:%m-%d}  {w.test_end:%Y-%m-%d}  {json.dumps(w.best_params):<45}"
            f"{float(tm['net_return_pct']):>10.2f}{float(tm['max_drawdown_pct']):>10.2f}"
            f"{float(sm['net_return_pct']):>9.2f}{float(sm['max_drawdown_pct']):>9.2f}{sm['trade_count']:>8}"
        )
    print(
        f"\nStitched OOS return {wf.stitched_test_return_pct:.2f}%  OOS MDD {wf.stitched_test_mdd_pct:.2f}%  "
        f"profitable OOS windows {wf.oos_profitable_windows}/{len(wf.windows)}"
    )
    print("param stability:", wf.param_stability)
    out = Path("../data/backtests")
    out.mkdir(parents=True, exist_ok=True)
    (out / "walk_forward.json").write_text(
        json.dumps(
            {
                "grid": grid,
                "train_days": args.train_days,
                "test_days": args.test_days,
                "base": args.base,
                "windows": [
                    {
                        "train_start": w.train_start.isoformat(),
                        "train_end": w.train_end.isoformat(),
                        "test_end": w.test_end.isoformat(),
                        "best_params": w.best_params,
                        "train": w.train_metrics,
                        "test": w.test_metrics,
                    }
                    for w in wf.windows
                ],
                "stitched_test_return_pct": str(wf.stitched_test_return_pct),
                "stitched_test_mdd_pct": str(wf.stitched_test_mdd_pct),
                "param_stability": wf.param_stability,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if wf.windows:
        w = wf.windows[-1]
        await repo.add_backtest_run(
            strategy=cfg.strategy.name,
            params={"grid": grid, "last_best": w.best_params},
            timeframe=cfg.strategy.timeframe,
            start=wf.windows[0].train_start,
            end=w.test_end,
            metrics={
                "stitched_test_return_pct": str(wf.stitched_test_return_pct),
                "stitched_test_mdd_pct": str(wf.stitched_test_mdd_pct),
            },
            equity_curve_path=str(out / "walk_forward.json"),
            is_walk_forward=True,
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
