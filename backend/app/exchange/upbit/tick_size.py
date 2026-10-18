"""KRW market order price units (docs.upbit.com/kr/docs/krw-market-info, checked 2026-09-24)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

# (lower bound inclusive, tick). Evaluated top-down.
_KRW_TICKS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("1000")),
    (Decimal("500000"), Decimal("500")),
    (Decimal("100000"), Decimal("100")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("1000"), Decimal("1")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
    (Decimal("0.001"), Decimal("0.00001")),
    (Decimal("0.0001"), Decimal("0.000001")),
    (Decimal("0.00001"), Decimal("0.0000001")),
    (Decimal("0"), Decimal("0.00000001")),
)

KRW_MIN_ORDER_TOTAL = Decimal("5000")


def krw_tick(price: Decimal) -> Decimal:
    if price < 0:
        raise ValueError("price must be non-negative")
    for lower, tick in _KRW_TICKS:
        if price >= lower:
            return tick
    return _KRW_TICKS[-1][1]


def round_price(price: Decimal, side: str = "down") -> Decimal:
    """Round to a valid tick. Buy limit prices should round down, sell limit prices up."""
    tick = krw_tick(price)
    rounding = ROUND_DOWN if side == "down" else ROUND_UP
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


def is_valid_price(price: Decimal) -> bool:
    tick = krw_tick(price)
    return (price % tick) == 0


def meets_min_total(
    price: Decimal, volume: Decimal, min_total: Decimal = KRW_MIN_ORDER_TOTAL
) -> bool:
    return price * volume >= min_total
