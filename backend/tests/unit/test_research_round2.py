from __future__ import annotations

from decimal import Decimal

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.metrics import TradeRecord
from app.backtest.research2 import concentration, round2_specs
from app.core.types import Timeframe
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams
from app.strategy.registry import build_strategy
from tests.conftest import trend_1m

CFG = BacktestConfig(
    Decimal(100000),
    FillParams(Decimal("0.0005"), Decimal(3)),
    SizingParams(Decimal("0.30"), Decimal(30000), Decimal(5000), Decimal("0.0005"), Decimal(1000)),
)


def _t(pnl: float) -> TradeRecord:
    return TradeRecord(
        "a",
        "b",
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(str(pnl)),
        Decimal(0),
        Decimal(0),
        1,
        "",
        "",
    )


def test_concentration_flags_single_winner_dependence() -> None:
    c = concentration([_t(5000), _t(-300), _t(-200), _t(-250), _t(-150)], 100000)
    assert c.trades == 5 and c.largest_win == 5000 and c.total_net_pnl == 4100
    assert c.top1_contribution > 1.0 and c.return_pct_ex_top1 < 0 and c.concentration_risk
    ok = concentration([_t(400)] * 10 + [_t(-100)] * 10, 100000)
    assert (
        not ok.concentration_risk
        and abs(ok.top3_contribution - 1200 / 3000) < 1e-9
        and ok.median_pnl == 150
    )


def test_concentration_no_trades_and_negative_total() -> None:
    assert concentration([], 100000).detail == "no trades"
    c = concentration([_t(100), _t(-500)], 100000)
    assert c.total_net_pnl == -400 and not c.concentration_risk and c.top1_contribution == 1.0


def test_round2_strategies_trade_on_1h_base() -> None:
    m1 = trend_1m(
        60 * 24 * 200,
        up_bars=60 * 24 * 12,
        down_bars=60 * 24 * 4,
        up_step=0.00003,
        down_step=-0.00004,
    )
    h1 = resample_1m(m1, Timeframe.H1)
    for spec in round2_specs():
        p = {**spec.base_params, "trend_ema": 20, "entry_n": 10, "exit_n": 5, "atr_period": 5}
        r = BacktestEngine(
            build_strategy(spec.name, spec.timeframe, spec.trend_timeframe, p), CFG
        ).run(h1)
        assert r.metrics.trade_count > 0, spec.key
        assert all(t.entry_reason.startswith("BREAKOUT") for t in r.trades)


def test_engine_start_at_skips_warmup_trades() -> None:
    m1 = trend_1m(
        60 * 24 * 120,
        up_bars=60 * 24 * 12,
        down_bars=60 * 24 * 4,
        up_step=0.00003,
        down_step=-0.00004,
    )
    h1 = resample_1m(m1, Timeframe.H1)
    p = {
        **round2_specs()[0].base_params,
        "trend_ema": 20,
        "entry_n": 10,
        "exit_n": 5,
        "atr_period": 5,
    }
    full = BacktestEngine(build_strategy("trend_following_4h", "4h", "1d", p), CFG).run(h1)
    start = h1[len(h1) // 2].open_time
    part = BacktestEngine(build_strategy("trend_following_4h", "4h", "1d", p), CFG).run(
        h1, start_at=start
    )
    assert part.start >= start and all(t.entry_time >= start.isoformat() for t in part.trades)
    assert part.metrics.trade_count <= full.metrics.trade_count and part.equity_curve[0][0] >= start
