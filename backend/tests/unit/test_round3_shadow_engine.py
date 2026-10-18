"""Round 3: ShadowEngine (incremental) == BacktestEngine (batch) trade-for-trade, incl. base switch."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.metrics import TradeRecord
from app.core.types import Candle, Timeframe
from app.market.candle_engine import resample_1m
from app.research.round3.config import DEFAULT_CONFIG, load_config
from app.research.round3.runner import backtest_config
from app.research.round3.shadow_engine import ShadowEngine

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def synthetic_1m(days: int, seed: int = 7) -> list[Candle]:
    """Trending random walk with regime flips — produces breakouts, stops and channel exits."""
    rng = random.Random(seed)
    px = 50_000_000.0
    drift = 0.00002
    out: list[Candle] = []
    for i in range(days * 1440):
        if i % (1440 * 9) == 0:
            drift = rng.choice([0.00004, -0.00003, 0.00001])
        o = px
        px *= 1 + drift + rng.gauss(0, 0.0006)
        hi, lo = (
            max(o, px) * (1 + abs(rng.gauss(0, 0.0003))),
            min(o, px) * (1 - abs(rng.gauss(0, 0.0003))),
        )

        def d(x: float) -> Decimal:
            return Decimal(str(round(x)))

        out.append(
            Candle(
                "KRW-BTC",
                Timeframe.M1,
                T0 + timedelta(minutes=i),
                d(o),
                d(hi),
                d(lo),
                d(px),
                Decimal("0.5"),
                d(px / 2),
            )
        )
    return out


def _key(t: TradeRecord) -> tuple[str, str, str, str, str, str, str]:
    return (
        t.entry_time,
        t.exit_time,
        str(t.entry_price),
        str(t.exit_price),
        str(t.quantity),
        str(t.net_pnl),
        t.exit_reason,
    )


@pytest.mark.parametrize("sid", ["D0", "D1", "D2", "D5"])
def test_shadow_equals_backtest_on_1h_base(sid: str) -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy(sid)
    h1 = resample_1m(synthetic_1m(200), Timeframe.H1)
    start_at = T0 + timedelta(days=60)
    bt = BacktestEngine(sd.build(), backtest_config(100_000)).run(h1, start_at=start_at)
    sh = ShadowEngine(
        sd.build(), backtest_config(100_000), start_at=start_at, base=Timeframe.H1, strategy_id=sid
    )
    events = [sh.on_bar(c) for c in h1]
    assert bt.metrics.trade_count >= 3, "synthetic series should produce trades"
    assert [_key(t) for t in sh.trades] == [_key(t) for t in bt.trades]
    assert sh.port.equity(h1[-1].close) == bt.metrics.ending_equity
    assert sum(len(e.decisions) for e in events) == sh.evaluations > 0
    fills = [f for e in events for f in e.fills]
    assert sum(1 for f in fills if f.side == "BUY") == len(bt.trades) + (
        1 if sh.port.has_position else 0
    )


def test_base_switch_1h_to_1m_flushes_boundary_buckets_and_stays_consistent() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy("D1")  # 2h strategy + 1d trend
    m1 = synthetic_1m(120)
    boundary = T0 + timedelta(days=90)
    start_at = boundary + timedelta(hours=5)
    h1 = resample_1m([c for c in m1 if c.open_time < boundary], Timeframe.H1)
    sh = ShadowEngine(
        sd.build(), backtest_config(100_000), start_at=start_at, base=Timeframe.H1, strategy_id="D1"
    )
    for c in h1:
        sh.on_bar(c)
    flushed = sh.switch_base(Timeframe.M1, boundary)
    assert {c.timeframe for c in flushed} == {Timeframe.H2, Timeframe.D1}
    assert all(c.close_time == boundary for c in flushed)
    for c in m1:
        if c.open_time >= boundary:
            sh.on_bar(c)
    # reference: the same strategy driven purely by 1m from the start (identical indicator history)
    ref = ShadowEngine(
        sd.build(),
        backtest_config(100_000),
        start_at=start_at,
        base=Timeframe.M1,
        strategy_id="ref",
    )
    for c in m1:
        ref.on_bar(c)
    assert sh.ctx.indicators[Timeframe.D1] == ref.ctx.indicators[Timeframe.D1]
    assert sh.ctx.indicators[Timeframe.H2] == ref.ctx.indicators[Timeframe.H2]
    assert [_key(t) for t in sh.trades] == [_key(t) for t in ref.trades]
    assert sh.port.equity(m1[-1].close) == ref.port.equity(m1[-1].close)


def test_switch_base_refuses_unaligned_boundary() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sh = ShadowEngine(
        cfg.strategy("D3").build(), backtest_config(100_000), start_at=T0, base=Timeframe.H1
    )
    for c in resample_1m(synthetic_1m(3), Timeframe.H1)[:30]:
        sh.on_bar(c)
    with pytest.raises(ValueError, match="cannot switch base"):
        sh.switch_base(Timeframe.M1, T0 + timedelta(hours=29))


def test_virtual_portfolio_never_exceeds_sizing_policy() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    sd = cfg.strategy("D6")
    sh = ShadowEngine(
        sd.build(),
        backtest_config(100_000),
        start_at=T0 + timedelta(days=30),
        base=Timeframe.H1,
        strategy_id="D6",
    )
    fills = [f for c in resample_1m(synthetic_1m(150), Timeframe.H1) for f in sh.on_bar(c).fills]
    buys = [f for f in fills if f.side == "BUY"]
    assert buys
    for f in buys:
        assert (
            f.fill.funds <= Decimal(30000) * (1 + Decimal("0.0003")) + 1
        )  # cap ₩30,000 (+slippage rounding)
        assert f.fill.funds >= Decimal(5000)
