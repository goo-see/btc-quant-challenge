"""Compounding capital management for the ETH grid LIVE EXPERIMENT.

    compounding_capital = STARTING_CAPITAL + NET REALIZED PNL   (unrealized PnL never counts)
    raw target unit     = compounding_capital x unit_ratio       (10 %: ₩5,000 of ₩50,000)
    unit                = floor(raw / ₩1,000) x ₩1,000, <= MAX_SINGLE_GRID_ORDER_KRW (hard cap)
    step-up limit       = at most +25 % of the previous unit per sizing update; decreases immediate
    unit < exchange minimum total -> COMPOUNDING_CAPITAL_TOO_LOW (new BUYs paused, ratio untouched)
Risk RATIOS never change: unit 10 %, max ETH inventory 80 %, min KRW reserve 20 % of the
conservative capital base. No leverage, no deposits, no cross-strategy transfer (rebalance OFF).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class CompoundingPolicy:
    enabled: bool
    unit_ratio: Decimal  # from the research candidate: unit_krw / budget_krw
    max_inventory_ratio: Decimal  # max_inventory_krw / budget_krw
    min_cash_ratio: Decimal  # min_cash_krw / budget_krw
    round_krw: Decimal = Decimal(1000)
    max_step_up_pct: Decimal = Decimal("0.25")
    max_single_order_krw: Decimal = Decimal(20000)
    min_total: Decimal = Decimal(5000)
    auto_rebalance: bool = False  # cross-strategy transfers require explicit user approval

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CompoundingPolicy:
        return cls(
            enabled=bool(d.get("enabled", False)),
            unit_ratio=Decimal(str(d["unit_ratio"])),
            max_inventory_ratio=Decimal(str(d["max_inventory_ratio"])),
            min_cash_ratio=Decimal(str(d["min_cash_ratio"])),
            round_krw=Decimal(str(d.get("round_krw", 1000))),
            max_step_up_pct=Decimal(str(d.get("max_step_up_pct", "0.25"))),
            max_single_order_krw=Decimal(str(d.get("max_single_order_krw", 20000))),
            min_total=Decimal(str(d.get("min_total", 5000))),
            auto_rebalance=bool(d.get("auto_rebalance", False)),
        )


def floor_krw(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return x.quantize(ONE, rounding=ROUND_DOWN)
    return (x / step).quantize(ONE, rounding=ROUND_DOWN) * step


def target_unit(capital: Decimal, policy: CompoundingPolicy) -> Decimal:
    """Unconstrained-by-step target: floor(capital x ratio, ₩1,000) capped at the hard maximum."""
    raw = max(ZERO, capital) * policy.unit_ratio
    return min(floor_krw(raw, policy.round_krw), policy.max_single_order_krw)


def next_unit(capital: Decimal, prev_unit: Decimal, policy: CompoundingPolicy) -> Decimal:
    """Unit for the next sizing epoch: increases are limited to +max_step_up_pct of the previous
    unit (then floored to the rounding step); decreases apply immediately."""
    t = target_unit(capital, policy)
    if prev_unit > 0 and t > prev_unit:
        cap = floor_krw(prev_unit * (ONE + policy.max_step_up_pct), policy.round_krw)
        t = min(t, max(cap, prev_unit))
    return t


def next_threshold(
    unit: Decimal, policy: CompoundingPolicy
) -> tuple[Decimal | None, Decimal | None]:
    """(next larger unit reachable in one step, capital where it is reached); None at the cap."""
    if unit >= policy.max_single_order_krw:
        return None, None
    nxt = min(
        unit + policy.round_krw,
        floor_krw(unit * (ONE + policy.max_step_up_pct), policy.round_krw),
        policy.max_single_order_krw,
    )
    if nxt <= unit:
        nxt = min(unit + policy.round_krw, policy.max_single_order_krw)
    if policy.unit_ratio <= 0:
        return nxt, None
    return nxt, (nxt / policy.unit_ratio).quantize(ONE)


def projected_units(
    policy: CompoundingPolicy,
    capitals: tuple[int, ...] = (60_000, 75_000, 100_000, 150_000, 200_000),
) -> list[dict[str, str]]:
    """'Order unit IF the capital reaches X' — never a forecast."""
    return [{"capital": str(c), "unit": str(target_unit(Decimal(c), policy))} for c in capitals]
