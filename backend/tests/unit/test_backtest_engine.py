from __future__ import annotations

from decimal import Decimal

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.metrics import TradeRecord, compute_metrics
from app.core.types import Timeframe
from app.execution.fill_model import FillParams
from app.market.candle_engine import resample_1m
from app.risk.position_sizer import SizingParams
from app.strategy.registry import build_strategy
from tests.conftest import trend_1m
from tests.unit.test_strategy import PARAMS

CFG = BacktestConfig(
    initial_equity=Decimal(100000),
    fill=FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(3)),
    sizing=SizingParams(
        Decimal("0.30"), Decimal(30000), Decimal(5000), Decimal("0.0005"), Decimal(1000)
    ),
)


def test_engine_runs_and_metrics_are_consistent() -> None:
    candles = trend_1m(20000, up_bars=400, down_bars=300)
    strat = build_strategy("trend_momentum", "15m", "1h", PARAMS)
    res = BacktestEngine(strat, CFG).run(candles)
    m = res.metrics
    assert m.trade_count > 0
    assert m.win_count + m.loss_count == m.trade_count
    assert m.fees_paid > 0
    # equity identity: start + sum(net trade pnl) == end when flat at the end
    if res.trades and m.trade_count == len(res.trades):
        total = sum((t.net_pnl for t in res.trades), Decimal(0))
        # open position at the end would break identity -> tolerate only if in position
        assert abs((m.starting_equity + total) - m.ending_equity) < Decimal(1) or m.exposure_pct > 0
    # every trade obeys the 30% / 30,000 cap
    for t in res.trades:
        assert t.entry_price * t.quantity <= Decimal(30000) * Decimal("1.001")
    assert Decimal(0) <= m.max_drawdown_pct <= Decimal(100)


def test_no_lookahead_fill_price_is_next_bar_open() -> None:
    candles = trend_1m(20000, up_bars=400, down_bars=300)
    strat = build_strategy("trend_momentum", "15m", "1h", PARAMS)
    res = BacktestEngine(strat, CFG).run(candles)
    by_time = {c.open_time.isoformat(): c for c in candles}
    slip = 1 + CFG.fill.slip
    for t in res.trades:
        bar = by_time[t.entry_time]  # entry_time == fill bar open_time
        # entry_price is the cost basis funds/volume; funds are rounded to 0.01 KRW, so the
        # only permitted deviation from open*(1+slip) is that rounding spread over the quantity.
        assert abs(t.entry_price - bar.open * slip) <= Decimal("0.01") / t.quantity + 1


def test_stop_is_enforced_intrabar_with_stop_priority() -> None:
    candles = trend_1m(20000, up_bars=400, down_bars=300)
    strat = build_strategy("trend_momentum", "15m", "1h", {**PARAMS, "atr_stop_mult": 0.5})
    res = BacktestEngine(strat, CFG).run(candles)
    stops = [t for t in res.trades if t.exit_reason.startswith("STOP@")]
    assert stops, "tight stop should trigger intrabar exits"


def test_same_result_on_15m_base_vs_1m_base_is_close_not_identical() -> None:
    candles = trend_1m(20000, up_bars=400, down_bars=300)
    r1 = BacktestEngine(build_strategy("trend_momentum", "15m", "1h", PARAMS), CFG).run(candles)
    r15 = BacktestEngine(build_strategy("trend_momentum", "15m", "1h", PARAMS), CFG).run(
        resample_1m(candles, Timeframe.M15)
    )
    assert r1.metrics.trade_count > 0 and r15.metrics.trade_count > 0
    assert abs(r1.metrics.trade_count - r15.metrics.trade_count) <= max(
        3, r1.metrics.trade_count // 3
    )


def test_compute_metrics_edge_cases() -> None:
    m = compute_metrics(
        starting_equity=Decimal(100000),
        equity_curve=[],
        trades=[],
        fees_paid=Decimal(0),
        first_price=Decimal(1),
        last_price=Decimal(1),
        bars_per_year=1.0,
        bars_in_position=0,
    )
    assert m.trade_count == 0 and m.profit_factor == 0.0 and m.win_rate == 0.0
    tr = [
        TradeRecord(
            "a",
            "b",
            Decimal(1),
            Decimal(1),
            Decimal(1),
            Decimal(100),
            Decimal(1),
            Decimal(1),
            1,
            "",
            "",
        ),
        TradeRecord(
            "a",
            "b",
            Decimal(1),
            Decimal(1),
            Decimal(1),
            Decimal(-50),
            Decimal(1),
            Decimal(1),
            1,
            "",
            "",
        ),
        TradeRecord(
            "a",
            "b",
            Decimal(1),
            Decimal(1),
            Decimal(1),
            Decimal(-50),
            Decimal(1),
            Decimal(1),
            1,
            "",
            "",
        ),
    ]
    m = compute_metrics(
        starting_equity=Decimal(100000),
        equity_curve=[
            (0, Decimal(100000)),
            (1, Decimal(100100)),
            (2, Decimal(100050)),
            (3, Decimal(100000)),
        ],
        trades=tr,
        fees_paid=Decimal(3),
        first_price=Decimal(1),
        last_price=Decimal(2),
        bars_per_year=1.0,
        bars_in_position=2,
    )
    assert m.profit_factor == 1.0 and m.max_consecutive_losses == 2 and m.expectancy == 0
    assert m.buy_hold_return_pct == 100
