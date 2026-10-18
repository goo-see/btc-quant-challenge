"""Position sizing — fixed fraction of equity, capped. NEVER scales up after losses.

Reused by the backtester (Day 1) and RiskManager (Day 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.portfolio.accounting import q_krw


@dataclass(frozen=True, slots=True)
class SizingParams:
    max_position_pct: Decimal  # fraction of current equity
    max_order_krw: Decimal
    min_order_krw: Decimal
    fee_rate: Decimal
    minimum_krw_balance: Decimal = Decimal(0)


def size_buy_krw(
    equity: Decimal, krw_available: Decimal, p: SizingParams
) -> tuple[Decimal | None, str]:
    """Return (krw_amount, reason). None when no valid order can be placed."""
    spendable = krw_available - p.minimum_krw_balance
    fee_room = spendable / (1 + p.fee_rate)
    amount = min(equity * p.max_position_pct, p.max_order_krw, fee_room)
    amount = q_krw(amount).quantize(Decimal(1))  # Upbit KRW amounts are whole won
    if amount < p.min_order_krw:
        return None, f"MIN_ORDER (amount={amount} < {p.min_order_krw})"
    return amount, "OK"
