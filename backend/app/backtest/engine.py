"""Event-driven backtester that reuses the live pipeline pieces.

Time semantics (no look-ahead):
  - Strategy sees only CLOSED candles; a signal at bar close is filled at the NEXT base bar's open.
  - Stops are enforced intrabar on each BASE bar (1m for final runs, 15m for fast sweeps) using
    FillModel.resolve_intrabar_exit -> STOP PRIORITY when ambiguous, gap -> open fill.
  - Higher-timeframe candles closing at the same instant are processed before the strategy TF.
Costs: fee + slippage on every fill; min order / whole-KRW rounding enforced by the sizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.backtest.metrics import Metrics, TradeRecord, compute_metrics
from app.core.types import Action, Candle, Fill, Signal, Timeframe
from app.execution.fill_model import FillModel, FillParams, ZeroVolumeError
from app.market.candle_engine import CandleAggregator
from app.market.indicators import IndicatorSet
from app.portfolio.accounting import PortfolioState
from app.risk.position_sizer import SizingParams, size_buy_krw
from app.strategy.base import BaseStrategy, PositionView, StrategyContext

MINUTES_PER_YEAR = 365 * 24 * 60


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: Decimal
    fill: FillParams
    sizing: SizingParams


@dataclass(slots=True)
class BacktestResult:
    metrics: Metrics
    trades: list[TradeRecord]
    equity_curve: list[tuple[datetime, Decimal]]
    signals: int
    rejected_buys: dict[str, int]
    start: datetime
    end: datetime
    params: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    def __init__(self, strategy: BaseStrategy, cfg: BacktestConfig) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.fill_model = FillModel(cfg.fill)

    def run(
        self, base_candles: list[Candle], *, start_at: datetime | None = None
    ) -> BacktestResult:
        """`start_at`: bars before it only warm up indicators/strategy state — no orders, no equity
        curve — so walk-forward windows can carry a warm-up prefix without look-ahead."""
        if not base_candles:
            raise ValueError("no candles")
        base_tf = base_candles[0].timeframe
        tfs = self.strategy.timeframes
        agg = CandleAggregator(tfs, base=base_tf)
        indicators: dict[Timeframe, IndicatorSet] = {
            tf: IndicatorSet.from_params(self.strategy.params) for tf in tfs
        }
        ctx = StrategyContext()
        port = PortfolioState(initial_equity=self.cfg.initial_equity, krw=self.cfg.initial_equity)
        equity_curve: list[tuple[datetime, Decimal]] = []
        trades: list[TradeRecord] = []
        pending: Signal | None = None
        stop_price: Decimal | None = None
        entry_reason = ""
        rejected: dict[str, int] = {}
        signals = 0
        bars_in_pos = 0
        order_seq = 0

        def _close_trade(fill: Fill, reason: str) -> None:
            nonlocal stop_price, entry_reason
            assert port.avg_entry is not None and port.entry_time is not None
            entry_price, entry_time, qty = port.avg_entry, port.entry_time, fill.volume
            entry_fees_before = port.entry_fees
            net = port.apply_sell(fill)
            trades.append(
                TradeRecord(
                    entry_time=entry_time.isoformat(),
                    exit_time=fill.timestamp.isoformat(),
                    entry_price=entry_price,
                    exit_price=fill.price,
                    quantity=qty,
                    net_pnl=net,
                    fees=fill.fee + entry_fees_before,
                    return_pct=(net / (entry_price * qty) * 100)
                    if entry_price * qty
                    else Decimal(0),
                    holding_minutes=int((fill.timestamp - entry_time).total_seconds() // 60),
                    entry_reason=entry_reason,
                    exit_reason=reason,
                )
            )
            stop_price = None
            entry_reason = ""

        for bar in base_candles:
            live = start_at is None or bar.open_time >= start_at
            # 1) execute pending signal at this bar's open (next-bar fill)
            if pending is not None and not live:
                pending = None
            if pending is not None:
                order_seq += 1
                cid = f"bt-{order_seq}"
                if pending.action is Action.BUY and not port.has_position:
                    amount, why = size_buy_krw(port.equity(bar.open), port.krw, self.cfg.sizing)
                    if amount is None:
                        rejected[why.split(" ")[0]] = rejected.get(why.split(" ")[0], 0) + 1
                    else:
                        try:
                            fill = self.fill_model.market_buy(cid, amount, bar.open, bar.open_time)
                        except ZeroVolumeError:
                            rejected["ZERO_VOLUME"] = rejected.get("ZERO_VOLUME", 0) + 1
                        else:
                            port.apply_buy(fill)
                            stop_price = pending.stop_price
                            entry_reason = pending.reason
                elif pending.action is Action.SELL and port.has_position:
                    fill = self.fill_model.market_sell(cid, port.btc, bar.open, bar.open_time)
                    _close_trade(fill, pending.reason)
                pending = None

            # 2) intrabar stop on this base bar (STOP priority policy inside FillModel)
            if port.has_position and stop_price is not None:
                ex = FillModel.resolve_intrabar_exit(bar, stop_price, None)
                if ex is not None:
                    order_seq += 1
                    fill = self.fill_model.market_sell(
                        f"bt-{order_seq}", port.btc, ex.price, bar.open_time
                    )
                    _close_trade(fill, f"STOP@{ex.price:.0f} ({ex.reason})")

            port.mark(bar.high if port.has_position else bar.close)
            port.mark(bar.close)
            if port.has_position and live:
                bars_in_pos += 1

            # 3) closed higher-TF candles (higher TF first so trend context is fresh)
            closed = agg.add(bar)
            if bar.timeframe in tfs:
                closed.append(bar)
            closed.sort(key=lambda c: -c.timeframe.minutes)
            for c in closed:
                ctx.indicators[c.timeframe] = indicators[c.timeframe].update(c)
                ctx.position = (
                    PositionView(
                        quantity=port.btc,
                        avg_entry=port.avg_entry or Decimal(0),
                        entry_time=port.entry_time or bar.open_time,
                        stop_price=stop_price,
                        highest_since_entry=port.highest_since_entry or bar.close,
                    )
                    if port.has_position
                    else None
                )
                sig = self.strategy.on_candle(c, ctx)
                if sig is None:
                    continue
                signals += 1
                if port.has_position and sig.stop_price is not None:
                    stop_price = max(stop_price or sig.stop_price, sig.stop_price)  # ratchet only
                if sig.action in (Action.BUY, Action.SELL) and live:
                    pending = sig
                if c.timeframe is self.strategy.timeframe and live:
                    equity_curve.append((c.close_time, port.equity(c.close)))

        # close any open position at the last close for reporting (marked, not a trade)
        last = base_candles[-1]
        if not equity_curve or equity_curve[-1][0] != last.close_time:
            equity_curve.append((last.close_time, port.equity(last.close)))

        bars_per_year = MINUTES_PER_YEAR / self.strategy.timeframe.minutes
        first_live = next(
            (c for c in base_candles if start_at is None or c.open_time >= start_at),
            base_candles[0],
        )
        metrics = compute_metrics(
            starting_equity=self.cfg.initial_equity,
            equity_curve=equity_curve,
            trades=trades,
            fees_paid=port.fees_total,
            first_price=first_live.open,
            last_price=last.close,
            bars_per_year=bars_per_year,
            bars_in_position=int(bars_in_pos * base_tf.minutes / self.strategy.timeframe.minutes),
        )
        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            signals=signals,
            rejected_buys=rejected,
            start=first_live.open_time,
            end=last.close_time,
            params=dict(self.strategy.params),
        )
