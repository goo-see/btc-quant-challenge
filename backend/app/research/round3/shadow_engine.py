"""Incremental shadow engine — the BacktestEngine loop, one base bar at a time.

Identical policy to app/backtest/engine.py (and therefore to the D0 research runs):
  - strategy sees CLOSED candles only; a BUY/SELL at bar close fills at the NEXT base bar's open
  - intrabar stop on every base bar via FillModel.resolve_intrabar_exit (STOP-first, gap -> open)
  - higher-TF candles closing at the same instant are processed before the strategy TF
  - same sizing (size_buy_krw), same fee/slippage (FillModel), same accounting (PortfolioState)
  - trailing stop ratchets only (max of old stop and new stop)
Bars before `start_at` only warm up indicators/strategy state (no orders, no records).
The base timeframe may be switched at an aligned UTC boundary (1h history -> live 1m stream);
open buckets are flushed only when they end exactly at that boundary.

tests/unit/test_round3_shadow_engine.py proves trade-for-trade equivalence with BacktestEngine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.backtest.engine import BacktestConfig
from app.backtest.metrics import TradeRecord
from app.core.types import Action, Candle, Fill, Signal, Timeframe
from app.execution.fill_model import FillModel, ZeroVolumeError
from app.market.candle_engine import CandleAggregator
from app.market.indicators import IndicatorSet
from app.portfolio.accounting import PortfolioState
from app.risk.position_sizer import size_buy_krw
from app.strategy.base import BaseStrategy, PositionView, StrategyContext


@dataclass(slots=True)
class DecisionEvent:
    signal: Signal
    bar: Candle  # the strategy-TF candle that was evaluated
    position_qty: Decimal
    entry_price: Decimal | None
    initial_stop: Decimal | None
    current_stop: Decimal | None  # stop in force BEFORE this bar's ratchet
    new_stop: Decimal | None  # stop in force AFTER this bar (ratcheted)
    equity: Decimal
    realized_pnl: Decimal


@dataclass(slots=True)
class FillEvent:
    fill: Fill
    side: str  # BUY | SELL
    reason: str  # signal reason or STOP@...
    kind: str  # SIGNAL | STOP
    signal_bar_close: datetime | None  # the decision this fill executes (None for stops)
    bar_open_time: datetime


@dataclass(slots=True)
class RejectEvent:
    reason: str
    signal_bar_close: datetime


@dataclass(slots=True)
class BarEvents:
    decisions: list[DecisionEvent] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    rejects: list[RejectEvent] = field(default_factory=list)


class ShadowEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        cfg: BacktestConfig,
        *,
        start_at: datetime,
        base: Timeframe,
        strategy_id: str = "",
    ) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.start_at = start_at
        self.strategy_id = strategy_id
        self.fill_model = FillModel(cfg.fill)
        self.tfs = strategy.timeframes
        self.base = base
        self.agg = CandleAggregator(self.tfs, base=base)
        self.indicators: dict[Timeframe, IndicatorSet] = {
            tf: IndicatorSet.from_params(strategy.params) for tf in self.tfs
        }
        self.ctx = StrategyContext()
        self.port = PortfolioState(initial_equity=cfg.initial_equity, krw=cfg.initial_equity)
        self.pending: Signal | None = None
        self.stop_price: Decimal | None = None
        self.initial_stop: Decimal | None = None
        self.entry_reason = ""
        self.order_seq = 0
        self.bars_seen = 0
        self.live_bars = 0
        self.evaluations = 0
        self.buy_signals = 0
        self.sell_signals = 0
        self.last_bar: Candle | None = None
        self.last_close_time: dict[Timeframe, datetime] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[datetime, Decimal]] = []

    # ------------------------------------------------------------------ stages
    def switch_base(self, new_base: Timeframe, boundary: datetime) -> list[Candle]:
        """Flush buckets that end exactly at `boundary` and continue on a finer base."""
        flushed = self.agg.flush()
        for c in flushed:
            if c.close_time != boundary:
                raise ValueError(
                    f"cannot switch base at {boundary}: open {c.timeframe} bucket ends {c.close_time}"
                )
        flushed.sort(key=lambda c: -c.timeframe.minutes)
        events = BarEvents()
        if self.last_bar is not None:
            self._process_closed(flushed, self.last_bar, live=False, events=events)
        self.base = new_base
        self.agg = CandleAggregator(self.tfs, base=new_base)
        return flushed

    # ------------------------------------------------------------------ core
    def on_bar(self, bar: Candle) -> BarEvents:
        if bar.timeframe is not self.base:
            raise ValueError(f"expected {self.base} bars, got {bar.timeframe}")
        events = BarEvents()
        live = bar.open_time >= self.start_at
        self.bars_seen += 1
        port = self.port

        # 1) execute pending signal at this bar's open (next-bar fill)
        if self.pending is not None and not live:
            self.pending = None
        if self.pending is not None:
            pending = self.pending
            self.order_seq += 1
            cid = f"sh-{self.strategy_id}-{self.order_seq}"
            if pending.action is Action.BUY and not port.has_position:
                amount, why = size_buy_krw(port.equity(bar.open), port.krw, self.cfg.sizing)
                if amount is None:
                    events.rejects.append(RejectEvent(why, pending.timestamp))
                else:
                    try:
                        fill = self.fill_model.market_buy(cid, amount, bar.open, bar.open_time)
                    except ZeroVolumeError:
                        events.rejects.append(RejectEvent("ZERO_VOLUME", pending.timestamp))
                    else:
                        port.apply_buy(fill)
                        self.stop_price = pending.stop_price
                        self.initial_stop = pending.stop_price
                        self.entry_reason = pending.reason
                        events.fills.append(
                            FillEvent(
                                fill,
                                "BUY",
                                pending.reason,
                                "SIGNAL",
                                pending.timestamp,
                                bar.open_time,
                            )
                        )
            elif pending.action is Action.SELL and port.has_position:
                fill = self.fill_model.market_sell(cid, port.btc, bar.open, bar.open_time)
                events.fills.append(
                    FillEvent(
                        fill, "SELL", pending.reason, "SIGNAL", pending.timestamp, bar.open_time
                    )
                )
                self._close_trade(fill, pending.reason, events)
            self.pending = None

        # 2) intrabar stop on this base bar (STOP priority inside FillModel)
        if port.has_position and self.stop_price is not None:
            ex = FillModel.resolve_intrabar_exit(bar, self.stop_price, None)
            if ex is not None:
                self.order_seq += 1
                fill = self.fill_model.market_sell(
                    f"sh-{self.strategy_id}-{self.order_seq}", port.btc, ex.price, bar.open_time
                )
                reason = f"STOP@{ex.price:.0f} ({ex.reason})"
                events.fills.append(FillEvent(fill, "SELL", reason, "STOP", None, bar.open_time))
                self._close_trade(fill, reason, events)

        port.mark(bar.high if port.has_position else bar.close)
        port.mark(bar.close)
        if live:
            self.live_bars += 1

        # 3) closed higher-TF candles (higher TF first so trend context is fresh)
        closed = self.agg.add(bar)
        if bar.timeframe in self.tfs:
            closed.append(bar)
        closed.sort(key=lambda c: -c.timeframe.minutes)
        self._process_closed(closed, bar, live=live, events=events)
        self.last_bar = bar
        return events

    def _process_closed(
        self, closed: list[Candle], bar: Candle, *, live: bool, events: BarEvents
    ) -> None:
        port = self.port
        for c in closed:
            self.ctx.indicators[c.timeframe] = self.indicators[c.timeframe].update(c)
            self.last_close_time[c.timeframe] = c.close_time
            self.ctx.position = (
                PositionView(
                    quantity=port.btc,
                    avg_entry=port.avg_entry or Decimal(0),
                    entry_time=port.entry_time or bar.open_time,
                    stop_price=self.stop_price,
                    highest_since_entry=port.highest_since_entry or bar.close,
                )
                if port.has_position
                else None
            )
            stop_before = self.stop_price
            sig = self.strategy.on_candle(c, self.ctx)
            if sig is None:
                continue
            if port.has_position and sig.stop_price is not None:
                self.stop_price = max(self.stop_price or sig.stop_price, sig.stop_price)
            if sig.action in (Action.BUY, Action.SELL) and live:
                self.pending = sig
            if c.timeframe is self.strategy.timeframe and live:
                self.evaluations += 1
                if sig.action is Action.BUY:
                    self.buy_signals += 1
                elif sig.action is Action.SELL:
                    self.sell_signals += 1
                self.equity_curve.append((c.close_time, port.equity(c.close)))
                events.decisions.append(
                    DecisionEvent(
                        signal=sig,
                        bar=c,
                        position_qty=port.btc,
                        entry_price=port.avg_entry,
                        initial_stop=self.initial_stop if port.has_position else None,
                        current_stop=stop_before if port.has_position else None,
                        new_stop=self.stop_price if port.has_position else None,
                        equity=port.equity(c.close),
                        realized_pnl=port.realized_pnl,
                    )
                )

    def _close_trade(self, fill: Fill, reason: str, events: BarEvents) -> None:
        port = self.port
        assert port.avg_entry is not None and port.entry_time is not None
        entry_price, entry_time, qty = port.avg_entry, port.entry_time, fill.volume
        entry_fees_before = port.entry_fees
        net = port.apply_sell(fill)
        tr = TradeRecord(
            entry_time=entry_time.isoformat(),
            exit_time=fill.timestamp.isoformat(),
            entry_price=entry_price,
            exit_price=fill.price,
            quantity=qty,
            net_pnl=net,
            fees=fill.fee + entry_fees_before,
            return_pct=(net / (entry_price * qty) * 100) if entry_price * qty else Decimal(0),
            holding_minutes=int((fill.timestamp - entry_time).total_seconds() // 60),
            entry_reason=self.entry_reason,
            exit_reason=reason,
        )
        self.trades.append(tr)
        events.trades.append(tr)
        self.stop_price = None
        self.initial_stop = None
        self.entry_reason = ""

    # ------------------------------------------------------------------ views
    def snapshot(self, price: Decimal | None = None) -> dict[str, Any]:
        port = self.port
        px = price if price is not None else (self.last_bar.close if self.last_bar else None)
        eq = port.equity(px) if px is not None else port.krw
        return {
            "cash": str(port.krw),
            "btc": str(port.btc),
            "entry_price": str(port.avg_entry) if port.avg_entry is not None else None,
            "entry_time": port.entry_time.isoformat() if port.entry_time else None,
            "initial_stop": str(self.initial_stop) if self.initial_stop is not None else None,
            "trailing_stop": str(self.stop_price) if self.stop_price is not None else None,
            "highest_since_entry": (
                str(port.highest_since_entry) if port.highest_since_entry is not None else None
            ),
            "realized_pnl": str(port.realized_pnl),
            "unrealized_pnl": str(port.unrealized_pnl(px)) if px is not None else "0",
            "equity": str(eq),
            "hwm": str(port.hwm),
            "max_drawdown_pct": str(port.max_drawdown * 100),
            "fees": str(port.fees_total),
            "trade_count": port.trade_count,
            "evaluations": self.evaluations,
            "buy_signals": self.buy_signals,
            "sell_signals": self.sell_signals,
            "pending": self.pending.action.value if self.pending else None,
            "last_bar_time": self.last_bar.open_time.isoformat() if self.last_bar else None,
        }
