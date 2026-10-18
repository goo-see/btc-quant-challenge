"""Run the configured strategy over stored 1m candles.

usage: uv run python ../scripts/run_backtest.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--base 1m|15m]
       [--param key=value ...] [--position-pct 0.3] [--name label]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import _bootstrap  # noqa: F401

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.report import format_metrics, save_result
from app.config.settings import Settings
from app.core.logging import configure_logging
from app.core.types import Timeframe, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams
from app.strategy.registry import build_strategy


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if s else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--base", default="1m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--position-pct", type=str, default=None)
    ap.add_argument("--name", default="baseline")
    args = ap.parse_args()

    settings = Settings.load()
    configure_logging("WARNING", json_output=False)
    cfg = settings.config
    params = dict(cfg.strategy.params)
    for kv in args.param:
        k, v = kv.split("=", 1)
        params[k] = float(v)

    engine = create_engine(cfg.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    base_tf = Timeframe(args.base)
    if base_tf is Timeframe.H1:  # long history (2017+) stored directly as 1h bars
        base = await repo.load_candles(
            cfg.system.market, Timeframe.H1, _dt(args.start), _dt(args.end)
        )
    else:
        candles = await repo.load_candles(
            cfg.system.market, Timeframe.M1, _dt(args.start), _dt(args.end)
        )
        base = candles if base_tf is Timeframe.M1 else resample_1m(candles, base_tf)
    if not base:
        raise SystemExit("no candles in DB — run scripts/fetch_history.py first")

    mode_risk = cfg.risk.for_mode(TradingMode.BACKTEST)
    pos_pct = Decimal(args.position_pct) if args.position_pct else mode_risk.max_position_pct
    bt_cfg = BacktestConfig(
        initial_equity=cfg.challenge.initial_capital_krw,
        fill=FillParams(fee_rate=cfg.risk.fee_rate, slippage_bps=cfg.risk.slippage_bps),
        sizing=SizingParams(
            max_position_pct=pos_pct,
            max_order_krw=mode_risk.max_order_krw,
            min_order_krw=cfg.risk.min_order_krw,
            fee_rate=cfg.risk.fee_rate,
            minimum_krw_balance=cfg.risk.minimum_krw_balance,
        ),
    )
    strat = build_strategy(
        cfg.strategy.name, cfg.strategy.timeframe, cfg.strategy.trend_timeframe, params
    )
    result = BacktestEngine(strat, bt_cfg).run(base)

    print(
        f"\n=== BACKTEST {cfg.strategy.name} tf={cfg.strategy.timeframe}/{cfg.strategy.trend_timeframe} base={args.base} pos_pct={pos_pct} ==="
    )
    print(f"params: {params}")
    print(format_metrics(result))
    paths = save_result(result, Path("../data/backtests"), args.name)
    run_id = await repo.add_backtest_run(
        strategy=cfg.strategy.name,
        params={**params, "position_pct": str(pos_pct), "base": args.base},
        timeframe=cfg.strategy.timeframe,
        start=result.start,
        end=result.end,
        metrics=result.metrics.to_dict(),
        equity_curve_path=paths["equity"],
    )
    print(f"saved: {paths} (backtest_runs.id={run_id})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
