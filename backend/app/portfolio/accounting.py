"""Pure portfolio accounting for a single long-only BTC position. Decimal everywhere.

Cost basis excludes fees (matches Upbit avg_buy_price); fees are tracked separately and
charged against realized PnL when the position (or part of it) is closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.core.types import Fill

ZERO = Decimal(0)
BTC_Q = Decimal("0.00000001")
KRW_Q = Decimal("0.01")


def q_btc(x: Decimal) -> Decimal:
    return x.quantize(BTC_Q, rounding=ROUND_DOWN)


def q_krw(x: Decimal) -> Decimal:
    return x.quantize(KRW_Q, rounding=ROUND_HALF_UP)  # fees round up: conservative


@dataclass(slots=True)
class PortfolioState:
    initial_equity: Decimal
    krw: Decimal
    btc: Decimal = ZERO
    avg_entry: Decimal | None = None
    entry_fees: Decimal = ZERO  # fees paid to open the current position
    entry_time: datetime | None = None
    highest_since_entry: Decimal | None = None
    realized_pnl: Decimal = ZERO
    fees_total: Decimal = ZERO
    hwm: Decimal = field(default=ZERO)
    max_drawdown: Decimal = ZERO  # as positive fraction, e.g. 0.12
    trade_count: int = 0

    def __post_init__(self) -> None:
        if self.hwm == ZERO:
            self.hwm = self.initial_equity

    # ------------------------------------------------------------- valuation
    @property
    def has_position(self) -> bool:
        return self.btc > ZERO

    def btc_value(self, price: Decimal) -> Decimal:
        return q_krw(self.btc * price)

    def equity(self, price: Decimal) -> Decimal:
        return q_krw(self.krw + self.btc * price)

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        if not self.has_position or self.avg_entry is None:
            return ZERO
        return q_krw((price - self.avg_entry) * self.btc - self.entry_fees)

    def total_pnl(self, price: Decimal) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl(price)

    def return_pct(self, price: Decimal) -> Decimal:
        return (self.equity(price) - self.initial_equity) / self.initial_equity

    def drawdown(self, price: Decimal) -> Decimal:
        eq = self.equity(price)
        if self.hwm <= ZERO:
            return ZERO
        return max(ZERO, (self.hwm - eq) / self.hwm)

    def mark(self, price: Decimal) -> None:
        """Update HWM / max drawdown / highest-since-entry. Call on every price update."""
        eq = self.equity(price)
        if eq > self.hwm:
            self.hwm = eq
        dd = self.drawdown(price)
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        if self.has_position and (
            self.highest_since_entry is None or price > self.highest_since_entry
        ):
            self.highest_since_entry = price

    # --------------------------------------------------------------- fills
    def apply_buy(self, fill: Fill) -> None:
        cost = fill.funds + fill.fee
        if cost > self.krw + KRW_Q:
            raise ValueError(f"insufficient KRW: need {cost}, have {self.krw}")
        new_btc = self.btc + fill.volume
        prev_cost = (self.avg_entry or ZERO) * self.btc
        self.avg_entry = (prev_cost + fill.funds) / new_btc
        self.btc = q_btc(new_btc)
        self.krw = q_krw(self.krw - cost)
        self.entry_fees += fill.fee
        self.fees_total += fill.fee
        if self.entry_time is None:
            self.entry_time = fill.timestamp
            self.highest_since_entry = fill.price

    def apply_sell(self, fill: Fill) -> Decimal:
        """Returns realized net PnL for the sold portion (after entry+exit fees)."""
        if fill.volume > self.btc + BTC_Q:
            raise ValueError(f"insufficient BTC: sell {fill.volume}, have {self.btc}")
        if self.avg_entry is None:
            raise ValueError("selling without cost basis")
        portion = fill.volume / self.btc if self.btc > ZERO else Decimal(1)
        entry_fee_part = q_krw(self.entry_fees * portion)
        gross = (fill.price - self.avg_entry) * fill.volume
        net = q_krw(gross - fill.fee - entry_fee_part)
        self.realized_pnl += net
        self.fees_total += fill.fee
        self.entry_fees -= entry_fee_part
        self.krw = q_krw(self.krw + fill.funds - fill.fee)
        self.btc = q_btc(self.btc - fill.volume)
        if self.btc <= BTC_Q:  # fully closed (dust tolerance)
            self.btc = ZERO
            self.avg_entry = None
            self.entry_fees = ZERO
            self.entry_time = None
            self.highest_since_entry = None
            self.trade_count += 1
        return net
