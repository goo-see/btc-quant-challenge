"""Strategy sleeves — logical capital isolation inside ONE exchange wallet.

- Each strategy gets an `allocated_capital`; sizing uses min(book equity, allocation) and the
  cash it may spend is min(book cash, allocation - position value): BTC_D0 can never spend the
  ETH_GRID reserve and vice versa.
- A position larger than the sleeve's sizing policy (opened under the previous single-strategy
  policy) is `grandfathered`: it is never reduced; only the NEXT entry uses the new sleeve size.
- Multi-asset reconciliation: exchange KRW == D0 book cash + sum(other ledgers' cash); exchange
  BTC == D0 qty; exchange ETH == ETH ledger qty. Normal trades of ANY strategy are therefore never
  mistaken for external flows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

ZERO = Decimal(0)


class SleeveConfig(BaseModel):
    asset: str  # BTC | ETH
    allocated_capital: Decimal
    strategy_version: str = ""
    mode: str = "LIVE"  # LIVE | LIVE_EXPERIMENT | SHADOW | PLANNED


class PortfolioConfig(BaseModel):
    enabled: bool = False
    sleeves: dict[str, SleeveConfig] = {}

    def for_strategy(self, strategy_version: str) -> tuple[str, SleeveConfig] | None:
        for sid, sc in self.sleeves.items():
            if sc.strategy_version == strategy_version:
                return sid, sc
        return None


LEDGER_KEY_PREFIX = "sleeve_ledger_"  # bot_state key per strategy: sleeve_ledger_<STRATEGY_ID>
LIVE_LEDGER_MODES = frozenset({"LIVE", "LIVE_EXPERIMENT"})  # modes that hold REAL assets


@dataclass(slots=True)
class SleeveLedger:
    """Cash + asset quantity owned by one strategy (the D0 sleeve is the trader's own book).

    `inflight` is set by the owning process while one of its orders is between submission and the
    ledger update, so the OTHER process' reconciliation can defer instead of failing on the
    sub-second window where the exchange already moved but the ledger has not."""

    strategy_id: str
    asset: str
    cash: Decimal
    quantity: Decimal
    mode: str = "LIVE"
    owner: str = ""
    inflight: bool = False
    updated_at: str = ""
    last_trade_at: str = ""  # when this ledger last changed because of a fill

    @property
    def holds_real_assets(self) -> bool:
        return self.mode in LIVE_LEDGER_MODES

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SleeveLedger:
        return cls(
            strategy_id=str(d["strategy_id"]),
            asset=str(d["asset"]),
            cash=Decimal(str(d.get("cash", "0"))),
            quantity=Decimal(str(d.get("quantity", "0"))),
            mode=str(d.get("mode", "LIVE")),
            owner=str(d.get("owner", "")),
            inflight=bool(d.get("inflight", False)),
            updated_at=str(d.get("updated_at", "")),
            last_trade_at=str(d.get("last_trade_at", "") or ""),
        )

    def key(self) -> str:
        return f"{LEDGER_KEY_PREFIX}{self.strategy_id}"


def sizing_caps(
    allocated: Decimal, book_equity: Decimal, book_cash: Decimal, position_value: Decimal
) -> tuple[Decimal, Decimal]:
    """(equity for sizing, cash available to this strategy)."""
    eq = min(book_equity, allocated)
    cash = min(book_cash, max(ZERO, allocated - position_value))
    return eq, cash


def sleeve_snapshot(
    strategy_id: str,
    cfg: SleeveConfig,
    *,
    cash: Decimal,
    quantity: Decimal,
    price: Decimal | None,
    realized_pnl: Decimal,
    fees: Decimal,
    entry_fees: Decimal = ZERO,
    avg_entry: Decimal | None = None,
    max_position_pct: Decimal = Decimal("0.30"),
) -> dict[str, Any]:
    position_value = (quantity * price) if (price is not None and quantity > 0) else ZERO
    unrealized = (
        ((price - avg_entry) * quantity - entry_fees)
        if (price is not None and avg_entry is not None and quantity > 0)
        else ZERO
    )
    eq_for_sizing, cash_avail = sizing_caps(
        cfg.allocated_capital, cash + position_value, cash, position_value
    )
    grandfathered = position_value > cfg.allocated_capital * max_position_pct * Decimal("1.05")
    return {
        "strategy_id": strategy_id,
        "asset": cfg.asset,
        "mode": cfg.mode,
        "allocated_capital": str(cfg.allocated_capital),
        "cash_reserved": str(max(ZERO, cfg.allocated_capital - position_value)),
        "cash_actual": str(cash),
        "position_quantity": str(quantity),
        "position_value": str(position_value),
        "realized_pnl": str(realized_pnl),
        "unrealized_pnl": str(unrealized),
        "fees": str(fees),
        "equity": str(cash_avail + position_value)
        if cfg.mode in LIVE_LEDGER_MODES
        else str(cash + position_value),
        "available_to_strategy": str(cash_avail),
        # policy maximum for the NEXT entry (allocation x max_position_pct); the live sizing uses
        # min(book equity, allocation) and is therefore never above it (never scaled up on losses)
        "next_max_entry_krw": str((cfg.allocated_capital * max_position_pct).quantize(Decimal(1))),
        "next_entry_estimate_krw": str((eq_for_sizing * max_position_pct).quantize(Decimal(1))),
        "grandfathered_position": grandfathered,
        "updated_at": datetime.now(UTC).isoformat(),
    }


@dataclass(slots=True)
class MultiAssetReconcile:
    ok: bool
    diffs: dict[str, Decimal] = field(default_factory=dict)
    expected: dict[str, Decimal] = field(default_factory=dict)
    exchange: dict[str, Decimal] = field(default_factory=dict)
    detail: str = ""


def reconcile_multi_asset(
    exchange: dict[str, Decimal],
    ledgers: list[SleeveLedger],
    *,
    krw_tolerance: Decimal,
    coin_tolerance: Decimal,
) -> MultiAssetReconcile:
    """exchange: {'KRW': total krw, 'BTC': qty, 'ETH': qty, ...}; ledgers: every LIVE ledger
    (shadow/planned ledgers hold no real assets and are ignored)."""
    live = [lg for lg in ledgers if lg.holds_real_assets]
    expected: dict[str, Decimal] = {"KRW": sum((lg.cash for lg in live), ZERO)}
    for lg in live:
        expected[lg.asset] = expected.get(lg.asset, ZERO) + lg.quantity
    assets = set(expected) | {k for k, v in exchange.items() if v > 0}
    diffs = {a: exchange.get(a, ZERO) - expected.get(a, ZERO) for a in assets}
    bad = [
        a for a, d in diffs.items() if abs(d) > (krw_tolerance if a == "KRW" else coin_tolerance)
    ]
    return MultiAssetReconcile(
        ok=not bad,
        diffs=diffs,
        expected=expected,
        exchange={a: exchange.get(a, ZERO) for a in assets},
        detail="" if not bad else "mismatch: " + ", ".join(f"{a} {diffs[a]:+}" for a in bad),
    )
