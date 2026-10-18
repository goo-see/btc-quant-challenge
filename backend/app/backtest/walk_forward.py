"""Walk-forward validation: rolling train/test windows; parameters chosen on train only.

Selection score deliberately penalises drawdown and thin samples:
    score = net_return_pct / max(max_drawdown_pct, 1.0)   if trade_count >= min_trades else -inf
Overfitting signal: test/train return ratio and how often the chosen params are profitable OOS.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from app.backtest.metrics import TradeRecord
from app.core.types import Candle
from app.strategy.registry import build_strategy


@dataclass(slots=True)
class WindowResult:
    train_start: datetime
    train_end: datetime
    test_end: datetime
    best_params: dict[str, float]
    train_metrics: dict[str, Any]
    test_metrics: dict[str, Any]
    train_score: float
    test_score: float
    test_trades: list[TradeRecord] = field(default_factory=list)
    test_buy_hold_return_pct: float = 0.0


@dataclass(slots=True)
class WalkForwardResult:
    windows: list[WindowResult]
    grid_size: int
    stitched_test_return_pct: Decimal
    stitched_test_mdd_pct: Decimal
    oos_profitable_windows: int
    param_stability: dict[str, int] = field(default_factory=dict)  # param json -> times chosen


def score(result: BacktestResult, min_trades: int) -> float:
    m = result.metrics
    if m.trade_count < min_trades:
        return -math.inf
    return float(m.net_return_pct) / max(float(m.max_drawdown_pct), 1.0)


def _slice(candles: list[Candle], start: datetime, end: datetime) -> list[Candle]:
    return [c for c in candles if start <= c.open_time < end]


def run_walk_forward(
    *,
    candles: list[Candle],
    strategy_name: str,
    timeframe: str,
    trend_timeframe: str | None,
    base_params: dict[str, float],
    grid: dict[str, list[float]],
    cfg: BacktestConfig,
    train_days: int,
    test_days: int,
    min_trades: int = 5,
    warmup_days: int = 0,
) -> WalkForwardResult:
    keys = list(grid)
    combos = [
        dict(zip(keys, vals, strict=True)) for vals in itertools.product(*(grid[k] for k in keys))
    ]
    start, end = candles[0].open_time, candles[-1].open_time
    windows: list[WindowResult] = []
    stitched: list[Decimal] = []  # per-window OOS return multipliers
    oos_curve: list[Decimal] = []
    eq = cfg.initial_equity
    chosen: dict[str, int] = {}

    t0 = start
    while t0 + timedelta(days=train_days + test_days) <= end + timedelta(minutes=1):
        t1 = t0 + timedelta(days=train_days)
        t2 = t1 + timedelta(days=test_days)
        warm = timedelta(days=warmup_days)
        train = _slice(candles, t0 - warm, t1)
        test = _slice(candles, t1 - warm, t2)
        if len(_slice(candles, t0, t1)) < 100 or len(_slice(candles, t1, t2)) < 20:
            break
        best: tuple[float, dict[str, float], BacktestResult] | None = None
        for combo in combos:
            params = {**base_params, **combo}
            strat = build_strategy(strategy_name, timeframe, trend_timeframe, params)
            res = BacktestEngine(strat, cfg).run(train, start_at=t0)
            s = score(res, min_trades)
            if best is None or s > best[0]:
                best = (s, params, res)
        assert best is not None
        s_train, best_params, train_res = best
        test_res = BacktestEngine(
            build_strategy(strategy_name, timeframe, trend_timeframe, best_params), cfg
        ).run(test, start_at=t1)
        test_live = _slice(candles, t1, t2)
        bh = (float(test_live[-1].close) / float(test_live[0].open) - 1) * 100 if test_live else 0.0
        windows.append(
            WindowResult(
                train_start=t0,
                train_end=t1,
                test_end=t2,
                best_params={k: best_params[k] for k in keys},
                train_metrics=train_res.metrics.to_dict(),
                test_metrics=test_res.metrics.to_dict(),
                train_score=s_train,
                test_score=score(test_res, 0),
                test_trades=list(test_res.trades),
                test_buy_hold_return_pct=bh,
            )
        )
        key = str(sorted((k, best_params[k]) for k in keys))
        chosen[key] = chosen.get(key, 0) + 1
        mult = test_res.metrics.ending_equity / test_res.metrics.starting_equity
        stitched.append(mult)
        for _, e in test_res.equity_curve:
            oos_curve.append(eq * (e / test_res.metrics.starting_equity))
        eq = eq * mult
        t0 = t0 + timedelta(days=test_days)  # roll forward by the test window length

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
        grid_size=len(combos),
        stitched_test_return_pct=(total_mult - 1) * 100,
        stitched_test_mdd_pct=mdd * 100,
        oos_profitable_windows=sum(
            1 for w in windows if Decimal(w.test_metrics["net_return_pct"]) > 0
        ),
        param_stability=chosen,
    )
