"""Exit-rule research engine — the BacktestEngine loop plus exit overlays and partial exits.

Identical to app/backtest/engine.py for entries and for the baseline exit stack (ATR stop,
trailing ratchet at 4H close, channel exit, trend-loss exit, STOP-first intrabar, next-base-bar
fill, same sizing/fees/slippage). Overlays (evaluated once per base bar AFTER that bar's stop
check and mark — i.e. a raised stop acts from the next bar; conservative at 1h resolution):
  - break-even / profit lock: when MFE >= trigger_r x R, stop = max(stop, lock price) where the
    lock price is fee+slippage adjusted so that the realised net PnL >= lock_r x R x qty
  - partial take-profit: when MFE >= trigger_r x R (once), sell `partial_frac` of the quantity at
    the next base bar open; the remainder keeps the baseline rules
R = avg entry fill price - initial stop carried on the BUY signal (per unit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_UP, Decimal
from typing import Any

from app.backtest.engine import BacktestConfig
from app.backtest.metrics import Metrics, TradeRecord, compute_metrics
from app.core.types import Action, Candle, Fill, Signal, Timeframe
from app.execution.fill_model import FillModel, ZeroVolumeError
from app.market.candle_engine import CandleAggregator
from app.market.indicators import IndicatorSet
from app.portfolio.accounting import PortfolioState
from app.risk.position_sizer import size_buy_krw
from app.strategy.base import PositionView, StrategyContext
from app.strategy.trend_following import TrendFollowing4h

MINUTES_PER_YEAR = 365 * 24 * 60


@dataclass(frozen=True, slots=True)
class ExitRule:
    key: str
    description: str
    trail_mult: float = 4.0  # strategy atr_trail_mult (entry rules do not depend on it)
    lock_trigger_r: float | None = None  # E1 1.0 / E4 1.5
    lock_r: float = 0.0  # E1 0.0 (break-even) / E4 0.5
    partial_trigger_r: float | None = None  # E3 2.0
    partial_frac: float = 0.5  # E3 50%

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "trail_mult": self.trail_mult,
            "lock_trigger_r": self.lock_trigger_r,
            "lock_r": self.lock_r,
            "partial_trigger_r": self.partial_trigger_r,
            "partial_frac": self.partial_frac,
        }


def exit_rules() -> list[ExitRule]:
    return [
        ExitRule(
            "E0", "CONTROL — current D0: 3xATR initial, 4xATR trail, 20-bar channel, trend loss"
        ),
        ExitRule(
            "E1",
            "Break-even protection: at MFE >= +1R stop >= fee/slippage-adjusted break-even",
            lock_trigger_r=1.0,
            lock_r=0.0,
        ),
        ExitRule("E2", "Tighter trailing: 3xATR trail (everything else = E0)", trail_mult=3.0),
        ExitRule(
            "E3",
            "Partial profit taking: at +2R sell 50%, remainder = E0 rules",
            partial_trigger_r=2.0,
            partial_frac=0.5,
        ),
        ExitRule(
            "E4",
            "Profit lock: at MFE >= +1.5R stop >= entry + 0.5R (fee/slippage adjusted)",
            lock_trigger_r=1.5,
            lock_r=0.5,
        ),
    ]


def lock_price(entry: Decimal, r: Decimal, lock_r: float, fee: Decimal, slip: Decimal) -> Decimal:
    """Smallest stop price whose STOP fill (stop*(1-slip), fee on proceeds) returns at least
    entry*(1+fee) + lock_r*R per unit, i.e. net PnL >= lock_r x R x qty after both fees."""
    target = entry * (1 + fee) + r * Decimal(str(lock_r))
    px = target / ((1 - slip) * (1 - fee))
    return px.quantize(Decimal(1), rounding=ROUND_UP)


@dataclass(slots=True)
class Leg:
    exit_time: datetime
    exit_price: Decimal
    quantity: Decimal
    reason: str
    net_pnl: Decimal
    fee: Decimal


@dataclass(slots=True)
class PositionRecord:
    entry_time: datetime
    entry_price: Decimal
    initial_stop: Decimal
    r_unit: Decimal
    quantity: Decimal
    entry_fee: Decimal
    entry_reason: str
    legs: list[Leg] = field(default_factory=list)
    mfe_price: Decimal = Decimal(0)
    mae_price: Decimal = Decimal(0)
    lock_applied_at: datetime | None = None
    partial_at: datetime | None = None

    @property
    def closed(self) -> bool:
        return bool(self.legs) and sum(
            (lg.quantity for lg in self.legs), Decimal(0)
        ) >= self.quantity - Decimal("0.00000001")

    @property
    def net_pnl(self) -> Decimal:
        return sum((lg.net_pnl for lg in self.legs), Decimal(0))

    @property
    def fees(self) -> Decimal:
        return self.entry_fee + sum((lg.fee for lg in self.legs), Decimal(0))

    def to_dict(self) -> dict[str, Any]:
        r = self.r_unit if self.r_unit > 0 else Decimal(1)
        basis = self.entry_price * self.quantity
        mfe_r = float((self.mfe_price - self.entry_price) / r) if self.mfe_price else 0.0
        mae_r = float((self.mae_price - self.entry_price) / r) if self.mae_price else 0.0
        realized_r = float(self.net_pnl / (r * self.quantity)) if self.quantity else 0.0
        final = self.legs[-1] if self.legs else None
        return {
            "entry_time": self.entry_time.isoformat(),
            "entry_price": float(self.entry_price),
            "initial_stop": float(self.initial_stop),
            "r_unit": float(self.r_unit),
            "r_pct": float(self.r_unit / self.entry_price * 100),
            "quantity": float(self.quantity),
            "exit_time": final.exit_time.isoformat() if final else None,
            "exit_price": float(final.exit_price) if final else None,
            "exit_reason": final.reason if final else None,
            "legs": [
                {
                    "exit_time": lg.exit_time.isoformat(),
                    "exit_price": float(lg.exit_price),
                    "quantity": float(lg.quantity),
                    "reason": lg.reason,
                    "net_pnl": float(lg.net_pnl),
                }
                for lg in self.legs
            ],
            "net_pnl": float(self.net_pnl),
            "return_pct": float(self.net_pnl / basis * 100) if basis else 0.0,
            "fees": float(self.fees),
            "holding_minutes": int((final.exit_time - self.entry_time).total_seconds() // 60)
            if final
            else None,
            "mfe_pct": float((self.mfe_price - self.entry_price) / self.entry_price * 100)
            if self.mfe_price
            else 0.0,
            "mae_pct": float((self.mae_price - self.entry_price) / self.entry_price * 100)
            if self.mae_price
            else 0.0,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "realized_r": realized_r,
            "giveback_r": (mfe_r - realized_r) if mfe_r > 0 else None,
            "lock_applied_at": self.lock_applied_at.isoformat() if self.lock_applied_at else None,
            "partial_at": self.partial_at.isoformat() if self.partial_at else None,
            "entry_reason": self.entry_reason,
        }


@dataclass(slots=True)
class ExitResult:
    metrics: Metrics
    trades: list[TradeRecord]  # one per position (entry -> final exit), pnl = sum of legs
    equity_curve: list[tuple[datetime, Decimal]]
    positions: list[PositionRecord]
    signals: int
    rejected_buys: dict[str, int]
    start: datetime
    end: datetime
    params: dict[str, Any] = field(default_factory=dict)


class ExitResearchEngine:
    def __init__(self, rule: ExitRule, params: dict[str, float], cfg: BacktestConfig) -> None:
        self.rule = rule
        self.cfg = cfg
        p = {**params, "atr_trail_mult": float(rule.trail_mult)}
        self.strategy = TrendFollowing4h(
            Timeframe.H4, Timeframe.D1, p
        )  # D0 class, entries unchanged
        self.fill_model = FillModel(cfg.fill)

    def run(self, base_candles: list[Candle], *, start_at: datetime | None = None) -> ExitResult:
        if not base_candles:
            raise ValueError("no candles")
        base_tf = base_candles[0].timeframe
        tfs = self.strategy.timeframes
        agg = CandleAggregator(tfs, base=base_tf)
        indicators = {tf: IndicatorSet.from_params(self.strategy.params) for tf in tfs}
        ctx = StrategyContext()
        port = PortfolioState(initial_equity=self.cfg.initial_equity, krw=self.cfg.initial_equity)
        fee, slip = self.cfg.fill.fee_rate, self.cfg.fill.slip
        equity_curve: list[tuple[datetime, Decimal]] = []
        positions: list[PositionRecord] = []
        trades: list[TradeRecord] = []
        cur: PositionRecord | None = None
        pending: Signal | None = None
        pending_partial = False
        stop_price: Decimal | None = None
        rejected: dict[str, int] = {}
        signals = 0
        bars_in_pos = 0
        seq = 0

        def _sell(fill: Fill, reason: str) -> None:
            nonlocal stop_price, cur
            assert cur is not None
            net = port.apply_sell(fill)
            cur.legs.append(Leg(fill.timestamp, fill.price, fill.volume, reason, net, fill.fee))
            if not port.has_position:
                trades.append(
                    TradeRecord(
                        entry_time=cur.entry_time.isoformat(),
                        exit_time=fill.timestamp.isoformat(),
                        entry_price=cur.entry_price,
                        exit_price=fill.price,
                        quantity=cur.quantity,
                        net_pnl=cur.net_pnl,
                        fees=cur.fees,
                        return_pct=(cur.net_pnl / (cur.entry_price * cur.quantity) * 100),
                        holding_minutes=int(
                            (fill.timestamp - cur.entry_time).total_seconds() // 60
                        ),
                        entry_reason=cur.entry_reason,
                        exit_reason=reason
                        if cur.partial_at is None
                        else f"{reason} (after PARTIAL_TP)",
                    )
                )
                positions.append(cur)
                cur = None
                stop_price = None

        for bar in base_candles:
            live = start_at is None or bar.open_time >= start_at
            if not live:
                pending, pending_partial = None, False
            # 1) pending fills at this bar's open
            if pending is not None or pending_partial:
                seq += 1
                cid = f"ex-{self.rule.key}-{seq}"
                if pending is not None and pending.action is Action.BUY and not port.has_position:
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
                            assert stop_price is not None and port.avg_entry is not None
                            cur = PositionRecord(
                                entry_time=fill.timestamp,
                                entry_price=port.avg_entry,
                                initial_stop=stop_price,
                                r_unit=port.avg_entry - stop_price,
                                quantity=fill.volume,
                                entry_fee=fill.fee,
                                entry_reason=pending.reason,
                                mfe_price=port.avg_entry,
                                mae_price=port.avg_entry,
                            )
                elif pending is not None and pending.action is Action.SELL and port.has_position:
                    fill = self.fill_model.market_sell(cid, port.btc, bar.open, bar.open_time)
                    _sell(fill, pending.reason)
                elif pending_partial and port.has_position and cur is not None:
                    qty = (port.btc * Decimal(str(self.rule.partial_frac))).quantize(
                        Decimal("0.00000001")
                    )
                    if qty > 0:
                        fill = self.fill_model.market_sell(cid, qty, bar.open, bar.open_time)
                        cur.partial_at = bar.open_time
                        _sell(fill, f"PARTIAL_TP@{self.rule.partial_trigger_r}R")
                pending, pending_partial = None, False

            # 2) intrabar stop (STOP-first), 3) mark + excursion tracking
            if port.has_position and stop_price is not None:
                ex = FillModel.resolve_intrabar_exit(bar, stop_price, None)
                if ex is not None:
                    seq += 1
                    fill = self.fill_model.market_sell(
                        f"ex-{self.rule.key}-{seq}", port.btc, ex.price, bar.open_time
                    )
                    _sell(fill, f"STOP@{ex.price:.0f} ({ex.reason})")
            port.mark(bar.high if port.has_position else bar.close)
            port.mark(bar.close)
            if port.has_position and cur is not None:
                cur.mfe_price = max(cur.mfe_price, bar.high)
                cur.mae_price = min(cur.mae_price, bar.low)
                if live:
                    bars_in_pos += 1
                # 4) overlays — act from the next bar on
                mfe_r = (
                    (cur.mfe_price - cur.entry_price) / cur.r_unit if cur.r_unit > 0 else Decimal(0)
                )
                if (
                    self.rule.lock_trigger_r is not None
                    and cur.lock_applied_at is None
                    and mfe_r >= Decimal(str(self.rule.lock_trigger_r))
                ):
                    lp = lock_price(cur.entry_price, cur.r_unit, self.rule.lock_r, fee, slip)
                    stop_price = max(stop_price or lp, lp)
                    cur.lock_applied_at = bar.open_time
                if (
                    self.rule.partial_trigger_r is not None
                    and cur.partial_at is None
                    and not pending_partial
                    and mfe_r >= Decimal(str(self.rule.partial_trigger_r))
                    and live
                ):
                    pending_partial = True

            # 5) closed higher-TF candles -> strategy (higher TF first)
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
                    stop_price = max(stop_price or sig.stop_price, sig.stop_price)
                if sig.action in (Action.BUY, Action.SELL) and live:
                    pending = sig
                if c.timeframe is self.strategy.timeframe and live:
                    equity_curve.append((c.close_time, port.equity(c.close)))

        last = base_candles[-1]
        if not equity_curve or equity_curve[-1][0] != last.close_time:
            equity_curve.append((last.close_time, port.equity(last.close)))
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
            bars_per_year=MINUTES_PER_YEAR / self.strategy.timeframe.minutes,
            bars_in_position=int(bars_in_pos * base_tf.minutes / self.strategy.timeframe.minutes),
        )
        return ExitResult(
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            positions=positions,
            signals=signals,
            rejected_buys=rejected,
            start=first_live.open_time,
            end=last.close_time,
            params={**dict(self.strategy.params), "exit_rule": self.rule.to_dict()},
        )
