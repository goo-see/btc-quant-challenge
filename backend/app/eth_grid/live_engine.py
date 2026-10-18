"""ETH grid LIVE EXPERIMENT engine — research candidate G0 (fixed 0.75% gap) with REAL fills.

The decision arithmetic is the research GridEngine's (app.research.eth_grid.engine):
  lattice L_k = anchor·(1+gap)^k; a ₩unit BUY when the price crosses DOWN through an empty level;
  every lot carries its own sell target = level·(1+gap) and is sold when the price crosses UP
  through it (a lot bought on the current observation never sells on it); seed inventory is
  laddered at anchor·(1+gap)^j; guards: max ETH inventory ₩40,000, min cash ₩10,000, daily (KST)
  sleeve loss ≤ −5% → buys paused for the day, sleeve MDD ≥ 12% → HALT (sells continue), daily
  close < EMA100 → buys paused (sells continue); re-centering moves only the buy lattice.
What differs from research: the engine emits an Intent instead of simulating a fill; the service
executes it (GridRisk -> OrderManager -> LiveGateway) and `apply_buy` / `apply_sell` book the
exchange's executed volume, funds and fee. Nothing in this module talks to the exchange.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Literal

from app.core.types import OrderRecord, Side
from app.eth_grid.compounding import (
    CompoundingPolicy,
    next_threshold,
    next_unit,
    projected_units,
    target_unit,
)
from app.exchange.upbit.tick_size import round_price
from app.research.eth_grid.engine import KST, GridParams, grid_candidates

Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")
ONE = Decimal(1)
ZERO = Decimal(0)

LIVE_CANDIDATE = "G0"


def live_params(candidate: str = LIVE_CANDIDATE) -> GridParams:
    """The frozen research candidate — ONE definition shared by backtest and LIVE."""
    for c in grid_candidates():
        if c.key == candidate:
            return c
    raise KeyError(f"unknown grid candidate {candidate}")


class GridStatus(StrEnum):
    WAITING_ENABLE = "WAITING_ENABLE"  # process up, experiment not enabled -> no orders
    SEEDING = "SEEDING"  # seed order in flight
    RUNNING = "RUNNING"
    BUY_PAUSED = "BUY_PAUSED"  # downtrend guard or daily-loss pause (sells continue)
    HALTED = "HALTED"  # MDD halt: no new buys, sells continue
    FROZEN = "FROZEN"  # integrity problem: no automatic orders at all


IntentKind = Literal["SEED", "GRID_BUY", "GRID_SELL"]


def _d(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, set):
        return sorted(v)
    return v


def _dt(v: Any) -> datetime:
    return datetime.fromisoformat(str(v))


@dataclass(slots=True)
class Lot:
    lot_id: int
    level: int | None  # lattice level bought at (None for seed lots)
    level_ref: Decimal  # lattice price at buy (anchor for seed) — duplicate-level suppression
    seed: bool
    buy_client_id: str
    buy_uuid: str | None
    buy_time: datetime
    buy_price: Decimal  # VWAP of the real fill
    quantity: Decimal
    buy_funds: Decimal
    buy_fee: Decimal
    target: Decimal
    entry_unit_krw: Decimal = Decimal(5000)  # order unit this lot was created with (never revised)
    sizing_epoch: str = "C0"
    strategy_epoch: str = "G0"  # grid rule set the lot was bought under (never revised)
    gap_pct: Decimal = Decimal("0.0075")  # gap used for this lot's target (never revised)
    exit_only: bool = False  # legacy lot: only its own SELL target may act on it

    @property
    def cost(self) -> Decimal:
        return self.buy_funds + self.buy_fee

    def to_dict(self) -> dict[str, Any]:
        return {k: _d(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lot:
        return cls(
            int(d["lot_id"]),
            d.get("level"),
            Decimal(str(d["level_ref"])),
            bool(d["seed"]),
            str(d["buy_client_id"]),
            d.get("buy_uuid"),
            _dt(d["buy_time"]),
            Decimal(str(d["buy_price"])),
            Decimal(str(d["quantity"])),
            Decimal(str(d["buy_funds"])),
            Decimal(str(d["buy_fee"])),
            Decimal(str(d["target"])),
            Decimal(str(d.get("entry_unit_krw", 5000))),
            str(d.get("sizing_epoch", "C0")),
            str(d.get("strategy_epoch", "G0")),
            Decimal(str(d.get("gap_pct", "0.0075"))),
            bool(d.get("exit_only", False)),
        )


@dataclass(slots=True)
class Cycle:
    lot_id: int
    level: int | None
    seed: bool
    buy_client_id: str
    buy_uuid: str | None
    buy_time: datetime
    buy_price: Decimal
    buy_quantity: Decimal
    buy_funds: Decimal
    buy_fee: Decimal
    sell_client_id: str
    sell_uuid: str | None
    sell_time: datetime
    sell_price: Decimal
    sell_quantity: Decimal
    sell_funds: Decimal
    sell_fee: Decimal
    gross_pnl: Decimal  # sell funds - buy funds (ex fees)
    net_pnl: Decimal  # sell funds - sell fee - (buy funds + buy fee)
    entry_unit_krw: Decimal = Decimal(5000)
    sizing_epoch: str = "C0"
    strategy_epoch: str = "G0"

    @property
    def duration_sec(self) -> float:
        return (self.sell_time - self.buy_time).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {k: _d(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Cycle:
        return cls(
            int(d["lot_id"]),
            d.get("level"),
            bool(d["seed"]),
            str(d["buy_client_id"]),
            d.get("buy_uuid"),
            _dt(d["buy_time"]),
            Decimal(str(d["buy_price"])),
            Decimal(str(d["buy_quantity"])),
            Decimal(str(d["buy_funds"])),
            Decimal(str(d["buy_fee"])),
            str(d["sell_client_id"]),
            d.get("sell_uuid"),
            _dt(d["sell_time"]),
            Decimal(str(d["sell_price"])),
            Decimal(str(d["sell_quantity"])),
            Decimal(str(d["sell_funds"])),
            Decimal(str(d["sell_fee"])),
            Decimal(str(d["gross_pnl"])),
            Decimal(str(d["net_pnl"])),
            Decimal(str(d.get("entry_unit_krw", 5000))),
            str(d.get("sizing_epoch", "C0")),
            str(d.get("strategy_epoch", "G0")),
        )


@dataclass(frozen=True, slots=True)
class Intent:
    kind: IntentKind
    side: Side
    ref_price: Decimal
    krw: Decimal | None = None
    quantity: Decimal | None = None
    level: int | None = None
    lot_id: int | None = None
    reason: str = ""


@dataclass(slots=True)
class PendingOrder:
    """Write-ahead record of the order being placed (persisted BEFORE the exchange call)."""

    client_order_id: str
    kind: IntentKind
    side: str
    ref_price: Decimal
    krw: Decimal | None
    quantity: Decimal | None
    level: int | None
    lot_id: int | None
    created_at: datetime
    unit_krw: Decimal = Decimal(5000)
    sizing_epoch: str = "C0"

    def to_dict(self) -> dict[str, Any]:
        return {k: _d(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingOrder:
        kind: IntentKind = d["kind"]
        return cls(
            str(d["client_order_id"]),
            kind,
            str(d["side"]),
            Decimal(str(d["ref_price"])),
            Decimal(str(d["krw"])) if d.get("krw") is not None else None,
            Decimal(str(d["quantity"])) if d.get("quantity") is not None else None,
            d.get("level"),
            d.get("lot_id"),
            _dt(d["created_at"]),
            Decimal(str(d.get("unit_krw", 5000))),
            str(d.get("sizing_epoch", "C0")),
        )


@dataclass(slots=True)
class GridEvent:
    ts: datetime
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


_DEC_FIELDS = {
    "cash",
    "anchor",
    "gap",
    "realized",
    "fees",
    "hwm",
    "mdd",
    "day_start_equity",
    "last_price",
    "seed_price",
    "unit_krw",
    "compounding_capital",
}


@dataclass(slots=True)
class LiveGridState:
    cash: Decimal
    lots: list[Lot] = field(default_factory=list)
    anchor: Decimal = ZERO
    gap: Decimal = ZERO
    next_lot_id: int = 1
    order_seq: int = 0
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    hwm: Decimal = ZERO
    mdd: Decimal = ZERO
    mdd_alerts: list[int] = field(default_factory=list)  # 5, 10 (% milestones announced)
    halted: bool = False
    halt_reason: str | None = None
    day_key: str = ""
    day_start_equity: Decimal = ZERO
    day_paused: bool = False
    day_buys: int = 0
    day_sells: int = 0
    trend_ok: bool = True
    ema: Decimal | None = None
    daily_close: Decimal | None = None
    last_price: Decimal = ZERO
    last_ts: datetime | None = None
    recenters: int = 0
    buys: int = 0
    sells: int = 0
    completed_cycles: int = 0
    level_blocked: set[int] = field(default_factory=set)
    lots_bought_now: set[int] = field(default_factory=set)
    pending: PendingOrder | None = None
    frozen: bool = False
    frozen_reason: str | None = None
    frozen_at: datetime | None = None
    started_at: datetime | None = None
    seeded_at: datetime | None = None
    seed_price: Decimal | None = None
    last_block_reason: str | None = None
    last_fill_at: datetime | None = None
    # compounding (ZERO unit = fixed research unit; set by LiveGridEngine.update_sizing)
    compounding_enabled: bool = False
    unit_krw: Decimal = ZERO
    compounding_capital: Decimal = ZERO
    sizing_epoch: str = "C0"
    sizing_epoch_n: int = 0
    sizing_history: list[dict[str, Any]] = field(default_factory=list)
    sizing_paused_reason: str | None = None
    # active grid rule set for NEW buys (legacy lots keep their own epoch/gap/target)
    strategy_epoch: str = "G0"
    cutover_history: list[dict[str, Any]] = field(default_factory=list)
    # operator pause of NEW buys only (sells of held lots keep working); None = not paused
    buy_pause_reason: str | None = None
    buy_pause_at: datetime | None = None
    buy_pause_by: str | None = None

    # ---------------------------------------------------------------- valuation
    @property
    def inventory_qty(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), ZERO)

    @property
    def inventory_cost(self) -> Decimal:
        return sum((lot.cost for lot in self.lots), ZERO)

    def inventory_value(self, price: Decimal) -> Decimal:
        return (self.inventory_qty * price).quantize(Q2)

    def equity(self, price: Decimal) -> Decimal:
        return (self.cash + self.inventory_value(price)).quantize(Q2)

    def unrealized(self, price: Decimal) -> Decimal:
        return (self.inventory_value(price) - self.inventory_cost).quantize(Q2)

    # ---------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if k in ("lots", "pending"):
                continue
            out[k] = _d(v)
        out["lots"] = [lot.to_dict() for lot in self.lots]
        out["pending"] = self.pending.to_dict() if self.pending else None
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LiveGridState:
        st = cls(cash=Decimal(str(d["cash"])))
        for k, v in d.items():
            if k in ("lots", "pending", "cash"):
                continue
            if k in _DEC_FIELDS:
                setattr(st, k, Decimal(str(v)) if v is not None else None)
            elif k in ("ema", "daily_close"):
                setattr(st, k, Decimal(str(v)) if v is not None else None)
            elif k in (
                "last_ts",
                "frozen_at",
                "started_at",
                "seeded_at",
                "last_fill_at",
                "buy_pause_at",
            ):
                setattr(st, k, _dt(v) if v else None)
            elif k in ("level_blocked", "lots_bought_now"):
                setattr(st, k, {int(x) for x in v})
            elif hasattr(st, k):
                setattr(st, k, v)
        st.lots = [Lot.from_dict(x) for x in d.get("lots", [])]
        st.pending = PendingOrder.from_dict(d["pending"]) if d.get("pending") else None
        return st


def policy_from_params(
    p: GridParams,
    *,
    enabled: bool,
    round_krw: Decimal = Decimal(1000),
    max_step_up_pct: Decimal = Decimal("0.25"),
    max_single_order_krw: Decimal = Decimal(20000),
    min_total: Decimal = Decimal(5000),
) -> CompoundingPolicy:
    """Ratios come from the frozen candidate (₩5,000 / ₩50,000 = 10 %, ₩40,000 / ₩50,000 = 80 %,
    ₩10,000 / ₩50,000 = 20 %) — never typed in by hand, never widened."""
    return CompoundingPolicy(
        enabled=enabled,
        unit_ratio=p.unit_krw / p.budget_krw,
        max_inventory_ratio=p.max_inventory_krw / p.budget_krw,
        min_cash_ratio=p.min_cash_krw / p.budget_krw,
        round_krw=round_krw,
        max_step_up_pct=max_step_up_pct,
        max_single_order_krw=max_single_order_krw,
        min_total=min_total,
    )


class LiveGridEngine:
    def __init__(
        self,
        p: GridParams,
        state: LiveGridState | None = None,
        *,
        policy: CompoundingPolicy | None = None,
    ) -> None:
        if p.gap_pct is None:
            raise ValueError("LIVE grid requires a fixed-gap candidate (G0)")
        self.p = p
        self.s = state or LiveGridState(cash=p.budget_krw)
        if self.s.gap == ZERO:
            self.s.gap = p.gap_pct
        self.cycles: list[Cycle] = []
        self.events: list[GridEvent] = []
        self.policy = policy
        self.s.compounding_enabled = bool(policy and policy.enabled)

    # ------------------------------------------------------------------ compounding
    @property
    def current_unit(self) -> Decimal:
        s = self.s
        if self.policy is not None and self.policy.enabled and s.unit_krw > 0:
            return s.unit_krw
        return self.p.unit_krw

    def realized_capital(self) -> Decimal:
        """STARTING_CAPITAL + net realized PnL (fees included in realized; unrealized excluded).
        Never includes anything outside this sleeve (no deposits, no BTC D0 cash)."""
        return (self.p.budget_krw + self.s.realized).quantize(Q2)

    def effective_limits(self, price: Decimal) -> tuple[Decimal, Decimal]:
        """(max ETH inventory, min KRW reserve). Fixed research amounts without compounding;
        with compounding the SAME ratios on a conservative base: inventory cap on
        min(realized capital, equity), cash reserve on max(realized capital, equity)."""
        if self.policy is None or not self.policy.enabled:
            return self.p.max_inventory_krw, self.p.min_cash_krw
        cap = self.realized_capital()
        eq = self.s.equity(price) if price > 0 else cap
        max_inv = (min(cap, eq) * self.policy.max_inventory_ratio).quantize(ONE)
        min_cash = (max(cap, eq) * self.policy.min_cash_ratio).quantize(ONE)
        return max_inv, min_cash

    def update_sizing(self, ts: datetime, reason: str) -> Decimal:
        """Recompute the order unit at a safe boundary (after a completed cycle / before a new
        BUY). Open lots keep their own entry_unit_krw; only NEW orders use the new unit."""
        s = self.s
        pol = self.policy
        if pol is None or not pol.enabled:
            return self.p.unit_krw
        capital = self.realized_capital()
        prev = s.unit_krw if s.unit_krw > 0 else self.p.unit_krw
        first = s.unit_krw == ZERO
        unit = self.p.unit_krw if first else next_unit(capital, prev, pol)
        if first:
            # epoch C0 = the fixed research unit; a bigger target applies from the next boundary
            unit = (
                min(self.p.unit_krw, target_unit(capital, pol))
                if target_unit(capital, pol) < self.p.unit_krw
                else self.p.unit_krw
            )
        s.compounding_capital = capital
        paused = unit < pol.min_total
        s.sizing_paused_reason = "COMPOUNDING_CAPITAL_TOO_LOW" if paused else None
        if first or unit != prev:
            if not first:
                s.sizing_epoch_n += 1
                s.sizing_epoch = f"C{s.sizing_epoch_n}"
            entry = {
                "epoch": s.sizing_epoch,
                "at": ts.isoformat(),
                "capital": str(capital),
                "unit": str(unit),
                "prev_unit": str(prev) if not first else None,
                "raw_target": str((capital * pol.unit_ratio).quantize(ONE)),
                "reason": reason,
                "paused": paused,
            }
            s.sizing_history = [*s.sizing_history, entry][-50:]
            s.unit_krw = unit
            if not first:
                self._emit(ts, "SIZING_UPDATE", **entry)
        else:
            s.unit_krw = unit
        return unit

    def compounding_view(self, price: Decimal) -> dict[str, Any]:
        pol = self.policy
        s = self.s
        unit = self.current_unit
        capital = self.realized_capital()
        eq = s.equity(price) if price > 0 else s.cash
        start = self.p.budget_krw
        base_unit = self.p.unit_krw
        bigger = [c for c in self.cycles if c.entry_unit_krw > base_unit]
        contribution = (
            str(
                sum(
                    (
                        (c.net_pnl * (c.entry_unit_krw - base_unit) / c.entry_unit_krw)
                        for c in bigger
                    ),
                    ZERO,
                ).quantize(Q2)
            )
            if bigger
            else None
        )
        max_inv, min_cash = self.effective_limits(price)
        out: dict[str, Any] = {
            "enabled": bool(pol and pol.enabled),
            "auto_rebalance": False,
            "starting_capital": str(start),
            "compounding_capital": str(capital),
            "current_equity": str(eq),
            "realized_net_pnl": str(s.realized),
            "unrealized_pnl": str(s.unrealized(price) if price > 0 else ZERO),
            "current_unit": str(unit),
            "base_unit": str(base_unit),
            "unit_ratio": str(pol.unit_ratio) if pol else str(base_unit / start),
            "max_single_order_krw": str(pol.max_single_order_krw) if pol else None,
            "max_inventory_ratio": str(pol.max_inventory_ratio)
            if pol
            else str(self.p.max_inventory_krw / start),
            "min_cash_ratio": str(pol.min_cash_ratio) if pol else str(self.p.min_cash_krw / start),
            "max_inventory_krw": str(max_inv),
            "min_cash_krw": str(min_cash),
            "round_krw": str(pol.round_krw) if pol else None,
            "max_step_up_pct": str(pol.max_step_up_pct) if pol else None,
            "sizing_epoch": s.sizing_epoch,
            "sizing_history": s.sizing_history[-10:],
            "paused_reason": s.sizing_paused_reason,
            "capital_growth_realized_pct": str(((capital - start) / start * 100).quantize(Q2)),
            "capital_growth_equity_pct": str(((eq - start) / start * 100).quantize(Q2)),
            "profit_reinvested": str(max(ZERO, capital - start).quantize(Q2)),
            "attribution": {
                "grid_realized_profit": str(s.realized),
                "unrealized_pnl": str(s.unrealized(price) if price > 0 else ZERO),
                "compounding_contribution": contribution,  # None -> N/A (no larger-unit cycles yet)
                "cycles_with_larger_unit": len(bigger),
            },
        }
        if pol and pol.enabled:
            nxt, thr = next_threshold(unit, pol)
            out.update(
                {
                    "next_unit": str(nxt) if nxt is not None else None,
                    "next_unit_threshold_capital": str(thr) if thr is not None else None,
                    "realized_needed_for_next_unit": str((thr - capital).quantize(Q2))
                    if thr is not None
                    else None,
                    "projected_units": projected_units(pol),
                }
            )
        return out

    # ------------------------------------------------------------------ helpers
    def _emit(self, ts: datetime, kind: str, **detail: Any) -> None:
        self.events.append(GridEvent(ts, kind, {k: _d(v) for k, v in detail.items()}))

    def drain_events(self) -> list[GridEvent]:
        out, self.events = self.events, []
        return out

    def level_price(self, k: int) -> Decimal:
        return (self.s.anchor * (ONE + self.s.gap) ** k).quantize(ONE)

    @property
    def status(self) -> GridStatus:
        s = self.s
        if s.frozen:
            return GridStatus.FROZEN
        if s.anchor == ZERO:
            return GridStatus.SEEDING if s.pending is not None else GridStatus.WAITING_ENABLE
        if s.halted:
            return GridStatus.HALTED
        if s.buy_pause_reason or s.day_paused or not s.trend_ok or s.sizing_paused_reason:
            return GridStatus.BUY_PAUSED
        return GridStatus.RUNNING

    # ------------------------------------------------------------------ strategy cutover
    @staticmethod
    def is_v2(epoch: str) -> bool:
        return epoch.startswith("AGGRESSIVE_V2")

    def active_lots(self) -> list[Lot]:
        """Lots that belong to the ACTIVE rule set (legacy EXIT_ONLY lots never block levels)."""
        return [lot for lot in self.s.lots if not lot.exit_only]

    def adopt_rules(self, epoch: str, gap: Decimal) -> bool:
        """Never-seeded engine: take the configured rule set directly (no lots, no history)."""
        if self.s.anchor != ZERO:
            return False
        self.s.strategy_epoch, self.s.gap = epoch, gap
        return True

    def cutover(
        self, ts: datetime, price: Decimal, new_epoch: str, new_gap: Decimal, legacy_label: str
    ) -> bool:
        """Switch NEW buys to another rule set at the current real price. Existing lots become
        EXIT_ONLY legacy lots (entry, quantity, cost basis and sell target untouched); the new
        lattice is anchored at `price` with last_price = price, so no level counts as 'crossed'
        until a real tick moves after activation. Never places an order. Idempotent."""
        s = self.s
        if s.strategy_epoch == new_epoch and s.gap == new_gap:
            return False
        prev_epoch, prev_gap, prev_anchor = s.strategy_epoch, s.gap, s.anchor
        if s.anchor != ZERO:
            for lot in s.lots:
                if lot.strategy_epoch != new_epoch:
                    lot.strategy_epoch = (
                        legacy_label if lot.strategy_epoch == prev_epoch else lot.strategy_epoch
                    )
                    lot.exit_only = True
            s.anchor = price
            s.last_price = price
            s.last_ts = ts
            s.level_blocked.clear()
            s.lots_bought_now.clear()
            s.last_block_reason = None
        s.strategy_epoch = new_epoch
        s.gap = new_gap
        entry = {
            "at": ts.isoformat(),
            "from_epoch": prev_epoch,
            "from_gap": str(prev_gap),
            "from_anchor": str(prev_anchor),
            "to_epoch": new_epoch,
            "to_gap": str(new_gap),
            "activation_price": str(price),
            "legacy_lots": len([lot for lot in s.lots if lot.exit_only]),
            "legacy_label": legacy_label,
        }
        s.cutover_history = [*s.cutover_history, entry][-20:]
        self._emit(ts, "CUTOVER", **entry)
        return True

    def buys_allowed(self, price: Decimal) -> tuple[bool, str]:
        """Identical to the research engine's guard order."""
        s = self.s
        if s.frozen:
            return False, "FROZEN"
        if s.halted:
            return False, "HALTED"
        if s.buy_pause_reason:
            return False, s.buy_pause_reason  # operator pause: NEW buys only, sells unaffected
        if not s.trend_ok:
            return False, "DOWNTREND_GUARD"
        if s.day_paused:
            return False, "DAILY_LOSS_PAUSE"
        if s.sizing_paused_reason:
            return False, s.sizing_paused_reason
        unit = self.current_unit
        max_inv, min_cash = self.effective_limits(price)
        if s.inventory_value(price) + unit > max_inv:
            return False, "MAX_INVENTORY"
        if s.cash - unit * (ONE + self.p.fee_rate) < min_cash:
            return False, "MIN_CASH_RESERVE"
        return True, "OK"

    def freeze(self, ts: datetime, reason: str) -> None:
        if not self.s.frozen:
            self.s.frozen, self.s.frozen_reason, self.s.frozen_at = True, reason, ts
            self._emit(ts, "FROZEN", reason=reason)

    def unfreeze(self, ts: datetime, by: str) -> None:
        if self.s.frozen:
            self.s.frozen, self.s.frozen_reason, self.s.frozen_at = False, None, None
            self._emit(ts, "UNFROZEN", by=by)

    def set_buy_pause(self, ts: datetime, reason: str | None, by: str = "operator") -> bool:
        """Operator switch: pause (reason) / resume (None) NEW buys. Never touches lots, targets,
        sizing state or sells. Returns True when the state changed."""
        s = self.s
        if (reason or None) == s.buy_pause_reason:
            return False
        if reason:
            s.buy_pause_reason, s.buy_pause_at, s.buy_pause_by = reason, ts, by
            s.last_block_reason = None
            self._emit(ts, "BUY_PAUSED", reason=reason, by=by, lots=len(s.lots))
        else:
            prev = s.buy_pause_reason
            s.buy_pause_reason, s.buy_pause_at, s.buy_pause_by = None, None, None
            self._emit(ts, "BUY_RESUMED", previous=prev, by=by)
        return True

    def set_trend(self, ts: datetime, daily_close: Decimal, ema: Decimal | None) -> None:
        s = self.s
        s.ema, s.daily_close = ema, daily_close
        ok = ema is None or daily_close > ema
        if ok != s.trend_ok:
            self._emit(ts, "GUARD_OFF" if ok else "GUARD_ON", close=daily_close, ema=ema)
        s.trend_ok = ok

    # ------------------------------------------------------------------ bookkeeping
    def _roll_day(self, ts: datetime, price: Decimal) -> None:
        s = self.s
        k = ts.astimezone(KST).strftime("%Y-%m-%d")
        if k != s.day_key:
            if s.day_key:
                self._emit(
                    ts,
                    "DAY_CLOSE",
                    day=s.day_key,
                    equity=s.equity(s.last_price or price),
                    buys=s.day_buys,
                    sells=s.day_sells,
                )
            s.day_key = k
            s.day_start_equity = s.equity(price)
            s.day_buys = s.day_sells = 0
            if s.day_paused:
                self._emit(ts, "RESUME", reason="new_day")
            s.day_paused = False

    def _bookkeeping(self, ts: datetime, price: Decimal) -> None:
        s = self.s
        eq = s.equity(price)
        s.hwm = max(s.hwm, eq)
        dd = (s.hwm - eq) / s.hwm if s.hwm > 0 else ZERO
        s.mdd = max(s.mdd, dd)
        for pct in (5, 10):
            if dd * 100 >= pct and pct not in s.mdd_alerts:
                s.mdd_alerts.append(pct)
                self._emit(ts, f"MDD_{pct}", drawdown=dd, equity=eq, hwm=s.hwm)
        if not s.halted and dd >= self.p.mdd_halt_pct:
            s.halted, s.halt_reason = True, f"MDD {dd:.2%}"
            self._emit(ts, "HALT", reason=s.halt_reason, equity=eq)
        if (
            not s.day_paused
            and s.day_start_equity > 0
            and (eq - s.day_start_equity) / s.day_start_equity <= -self.p.daily_loss_pause_pct
        ):
            s.day_paused = True
            self._emit(ts, "PAUSE", reason="DAILY_LOSS", equity=eq, day_start=s.day_start_equity)

    def _recenter(self, ts: datetime, price: Decimal, reason: str) -> None:
        s = self.s
        s.anchor = price
        s.level_blocked.clear()
        s.recenters += 1
        self._emit(ts, "RECENTER", anchor=price, gap=s.gap, lots=len(s.lots), reason=reason)

    # ------------------------------------------------------------------ decisions
    def next_intent(
        self,
        ts: datetime,
        price: Decimal,
        *,
        enabled: bool,
        skip_lots: frozenset[int] = frozenset(),
        skip_levels: frozenset[int] = frozenset(),
    ) -> Intent | None:
        """The next order this observation requires, or None. Call repeatedly (after each fill)
        until None, then `end_observation`. Order of evaluation = research on_price:
        sells -> bookkeeping -> re-center -> buys (levels crossed downward since the last price)."""
        s = self.s
        if s.frozen or s.pending is not None:
            return None
        if s.anchor == ZERO:
            if not enabled:
                return None
            return Intent("SEED", Side.BID, price, krw=self.p.seed_krw, reason="INITIAL_SEED")
        self._roll_day(ts, price)
        prev = s.last_price or price
        for lot in sorted(
            (
                lot
                for lot in s.lots
                if price >= lot.target
                and lot.lot_id not in s.lots_bought_now
                and lot.lot_id not in skip_lots
            ),
            key=lambda lot: lot.target,
        ):
            return Intent(
                "GRID_SELL",
                Side.ASK,
                price,
                quantity=lot.quantity,
                level=lot.level,
                lot_id=lot.lot_id,
                reason=f"TARGET@{lot.target:.0f}",
            )
        self._bookkeeping(ts, price)
        lvl = self.p.levels_each_side
        if price > self.level_price(lvl) or price < self.level_price(-lvl):
            self._recenter(ts, price, "OUT_OF_RANGE")
        if price < prev:
            self.update_sizing(ts, "BEFORE_BUY")
            for k in range(lvl, -lvl - 1, -1):
                lp = self.level_price(k)
                if (
                    lp >= prev
                    or lp < price
                    or k in s.level_blocked
                    or k in skip_levels
                    or lp >= s.anchor
                ):
                    continue
                if any(abs(lot.level_ref / lp - ONE) < s.gap / 2 for lot in self.active_lots()):
                    continue  # a lot of the ACTIVE rule set already sits at this level
                ok, why = self.buys_allowed(lp)
                if not ok:
                    if why != s.last_block_reason:
                        self._emit(ts, "BUY_BLOCKED", level=k, price=lp, reason=why)
                    s.last_block_reason = why
                    break
                s.last_block_reason = None
                return Intent(
                    "GRID_BUY",
                    Side.BID,
                    price,
                    krw=self.current_unit,
                    level=k,
                    reason=f"LEVEL{k:+d}@{lp:.0f}",
                )
        return None

    def end_observation(self, ts: datetime, price: Decimal) -> None:
        s = self.s
        if s.anchor != ZERO:
            s.last_price = price
        s.last_ts = ts
        s.lots_bought_now.clear()

    # ------------------------------------------------------------------ orders
    def mark_pending(self, intent: Intent, client_order_id: str, ts: datetime) -> PendingOrder:
        s = self.s
        if s.pending is not None:
            raise RuntimeError("an order is already pending")
        s.order_seq += 1
        s.pending = PendingOrder(
            client_order_id,
            intent.kind,
            intent.side.value,
            intent.ref_price,
            intent.krw,
            intent.quantity,
            intent.level,
            intent.lot_id,
            ts,
            intent.krw if (intent.kind == "GRID_BUY" and intent.krw) else self.p.unit_krw,
            s.sizing_epoch,
        )
        return s.pending

    def clear_pending(self, ts: datetime, reason: str) -> None:
        if self.s.pending is not None:
            self._emit(
                ts,
                "ORDER_NOT_FILLED",
                client_order_id=self.s.pending.client_order_id,
                reason=reason,
            )
        self.s.pending = None

    @staticmethod
    def _fill_totals(rec: OrderRecord) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        qty = rec.executed_volume
        funds = sum((f.funds for f in rec.fills), ZERO)
        if funds == ZERO and rec.avg_fill_price is not None:
            funds = (qty * rec.avg_fill_price).quantize(Q2)
        fee = rec.paid_fee if rec.paid_fee else sum((f.fee for f in rec.fills), ZERO)
        vwap = (funds / qty) if qty > 0 else ZERO
        return qty, funds.quantize(Q2), fee.quantize(Q2), vwap

    def apply_buy(self, rec: OrderRecord, ts: datetime) -> list[Lot]:
        """Book a REAL buy fill against the pending order. Seed fills are split into ₩unit lots
        (sum of lot quantities == executed volume; sum of lot costs == funds + fee)."""
        s = self.s
        pend = s.pending
        if pend is None or pend.client_order_id != rec.client_order_id or pend.side != "bid":
            raise RuntimeError("apply_buy without a matching pending buy")
        qty, funds, fee, vwap = self._fill_totals(rec)
        if qty <= 0:
            self.clear_pending(ts, f"no_fill state={rec.state.value}")
            return []
        lots: list[Lot] = []
        if pend.kind == "SEED":
            s.anchor = pend.ref_price
            s.gap = self.p.gap_pct or s.gap
            n = max(1, int(self.p.seed_krw / self.p.unit_krw))
            unit_q = (qty / n).quantize(Q8, rounding=ROUND_DOWN)
            cost_total = funds + fee
            acc_q = acc_funds = acc_fee = ZERO
            for j in range(1, n + 1):
                q = unit_q if j < n else (qty - acc_q)
                f_ = (funds * q / qty).quantize(Q2) if j < n else (funds - acc_funds)
                fe = (fee * q / qty).quantize(Q2) if j < n else (fee - acc_fee)
                acc_q, acc_funds, acc_fee = acc_q + q, acc_funds + f_, acc_fee + fe
                lots.append(
                    Lot(
                        s.next_lot_id,
                        None,
                        s.anchor,
                        True,
                        rec.client_order_id,
                        rec.exchange_uuid,
                        ts,
                        vwap,
                        q,
                        f_,
                        fe,
                        (s.anchor * (ONE + s.gap) ** j).quantize(ONE),
                        self.p.unit_krw,
                        pend.sizing_epoch,
                        s.strategy_epoch,
                        s.gap,
                    )
                )
                s.next_lot_id += 1
            assert sum((lot.quantity for lot in lots), ZERO) == qty
            assert sum((lot.cost for lot in lots), ZERO) == cost_total
            s.seeded_at, s.seed_price = ts, pend.ref_price
            s.day_key = ts.astimezone(KST).strftime("%Y-%m-%d")
        else:
            assert pend.level is not None
            lp = self.level_price(pend.level)
            # V2: sell target = ACTUAL fill x (1 + gap), rounded UP to the exchange tick;
            # legacy G0 rule: level price x (1 + gap)
            target = (
                round_price(vwap * (ONE + s.gap), "up")
                if self.is_v2(s.strategy_epoch)
                else (lp * (ONE + s.gap)).quantize(ONE)
            )
            lots.append(
                Lot(
                    s.next_lot_id,
                    pend.level,
                    lp,
                    False,
                    rec.client_order_id,
                    rec.exchange_uuid,
                    ts,
                    vwap,
                    qty,
                    funds,
                    fee,
                    target,
                    pend.unit_krw,
                    pend.sizing_epoch,
                    s.strategy_epoch,
                    s.gap,
                )
            )
            s.next_lot_id += 1
            s.level_blocked.add(pend.level)
        s.lots.extend(lots)
        s.last_fill_at = ts
        s.cash = (s.cash - funds - fee).quantize(Q2)
        s.fees += fee
        s.buys += 1
        s.day_buys += 1
        s.lots_bought_now.update(lot.lot_id for lot in lots)
        if pend.kind == "SEED":
            eq = s.equity(pend.ref_price)
            s.hwm = max(s.hwm, eq)
            s.day_start_equity = eq
            s.last_price = pend.ref_price
            self._emit(
                ts,
                "SEED",
                client_order_id=rec.client_order_id,
                uuid=rec.exchange_uuid,
                anchor=s.anchor,
                gap=s.gap,
                qty=qty,
                fill=vwap,
                funds=funds,
                fee=fee,
                lots=len(lots),
                cash=s.cash,
                targets=[str(lot.target) for lot in lots],
            )
        else:
            self._emit(
                ts,
                "BUY",
                client_order_id=rec.client_order_id,
                uuid=rec.exchange_uuid,
                level=pend.level,
                ref=pend.ref_price,
                fill=vwap,
                qty=qty,
                funds=funds,
                fee=fee,
                target=lots[0].target,
                cash=s.cash,
            )
        s.pending = None
        return lots

    def apply_sell(self, rec: OrderRecord, ts: datetime) -> Cycle | None:
        s = self.s
        pend = s.pending
        if pend is None or pend.client_order_id != rec.client_order_id or pend.side != "ask":
            raise RuntimeError("apply_sell without a matching pending sell")
        lot = next((lot for lot in s.lots if lot.lot_id == pend.lot_id), None)
        if lot is None:
            raise RuntimeError(f"pending sell references unknown lot {pend.lot_id}")
        sold, funds, fee, vwap = self._fill_totals(rec)
        if sold <= 0:
            self.clear_pending(ts, f"no_fill state={rec.state.value}")
            return None
        if sold < lot.quantity - Q8:
            # partial: the sold part becomes a cycle, the remainder stays a (smaller) lot
            frac = sold / lot.quantity
            b_funds = (lot.buy_funds * frac).quantize(Q2)
            b_fee = (lot.buy_fee * frac).quantize(Q2)
            lot.quantity = (lot.quantity - sold).quantize(Q8)
            lot.buy_funds -= b_funds
            lot.buy_fee -= b_fee
            self._emit(ts, "PARTIAL_SELL", lot_id=lot.lot_id, sold=sold, remaining=lot.quantity)
        else:
            b_funds, b_fee = lot.buy_funds, lot.buy_fee
            s.lots.remove(lot)
            if lot.level is not None:
                s.level_blocked.discard(lot.level)
        net = (funds - fee - (b_funds + b_fee)).quantize(Q2)
        cyc = Cycle(
            lot.lot_id,
            lot.level,
            lot.seed,
            lot.buy_client_id,
            lot.buy_uuid,
            lot.buy_time,
            lot.buy_price,
            sold,
            b_funds,
            b_fee,
            rec.client_order_id,
            rec.exchange_uuid,
            ts,
            vwap,
            sold,
            funds,
            fee,
            (funds - b_funds).quantize(Q2),
            net,
            lot.entry_unit_krw,
            lot.sizing_epoch,
            lot.strategy_epoch,
        )
        s.last_fill_at = ts
        s.cash = (s.cash + funds - fee).quantize(Q2)
        s.fees += fee
        s.realized += net
        s.sells += 1
        s.day_sells += 1
        s.completed_cycles += 1
        self.cycles.append(cyc)
        s.pending = None
        self.update_sizing(ts, "AFTER_CYCLE")
        self._emit(
            ts,
            "SELL",
            client_order_id=rec.client_order_id,
            uuid=rec.exchange_uuid,
            lot_id=lot.lot_id,
            level=lot.level,
            target=lot.target,
            fill=vwap,
            qty=sold,
            funds=funds,
            fee=fee,
            net=net,
            cash=s.cash,
            cycles=s.completed_cycles,
        )
        return cyc

    # ------------------------------------------------------------------ views
    def ladder(self, price: Decimal, buy_levels: int = 3) -> dict[str, Any]:
        s = self.s
        pend = s.pending
        sells = [
            {
                "price": str(lot.target),
                "side": "SELL",
                "amount": str((lot.quantity * lot.target).quantize(ONE)),
                "quantity": str(lot.quantity),
                "lot_id": lot.lot_id,
                "level": lot.level,
                "seed": lot.seed,
                "state": "OPEN" if (pend and pend.lot_id == lot.lot_id) else "WAIT",
                "strategy_epoch": lot.strategy_epoch,
                "gap_pct": str(lot.gap_pct),
                "legacy": lot.exit_only,
            }
            for lot in sorted(s.lots, key=lambda lot: lot.target)
        ]
        buys: list[dict[str, Any]] = []
        if s.anchor != ZERO and not s.buy_pause_reason:
            for k in range(self.p.levels_each_side, -self.p.levels_each_side - 1, -1):
                lp = self.level_price(k)
                if lp >= price or lp >= s.anchor or k in s.level_blocked:
                    continue
                if any(abs(lot.level_ref / lp - ONE) < s.gap / 2 for lot in self.active_lots()):
                    continue
                buys.append(
                    {
                        "price": str(lp),
                        "side": "BUY",
                        "amount": str(self.current_unit),
                        "level": k,
                        "strategy_epoch": s.strategy_epoch,
                        "gap_pct": str(s.gap),
                        "state": "OPEN"
                        if (pend and pend.level == k and pend.side == "bid")
                        else "WAIT",
                    }
                )
                if len(buys) >= buy_levels:
                    break
        return {
            "price": str(price),
            "anchor": str(s.anchor),
            "gap_pct": str(s.gap),
            "strategy_epoch": s.strategy_epoch,
            "legacy_lots": len([lot for lot in s.lots if lot.exit_only]),
            "sells": sells,
            "buys": buys,
            "buys_allowed": self.buys_allowed(price)[1] if s.anchor != ZERO else "NOT_SEEDED",
        }

    def book(self, price: Decimal) -> dict[str, Any]:
        """LIVE performance book — everything from REAL fills; GRID TOTAL EQUITY is the KPI."""
        s = self.s
        px = price if price > 0 else s.last_price
        eq = s.equity(px) if px else s.cash
        inv_v = s.inventory_value(px) if px else ZERO
        nets = [float(c.net_pnl) for c in self.cycles]
        durs = [c.duration_sec for c in self.cycles]
        start = self.p.budget_krw
        return {
            "starting_equity": str(start),
            "current_equity": str(eq),
            "high_watermark": str(s.hwm),
            "mdd_pct": str((s.mdd * 100).quantize(Q2)),
            "current_drawdown_pct": str(
                (((s.hwm - eq) / s.hwm * 100) if s.hwm > 0 else ZERO).quantize(Q2)
            ),
            "realized_pnl": str(s.realized),
            "unrealized_pnl": str(s.unrealized(px) if px else ZERO),
            "total_fees": str(s.fees),
            "gross_grid_profit": str(sum((c.gross_pnl for c in self.cycles), ZERO)),
            "net_profit": str((eq - start).quantize(Q2)),
            "return_pct": str(((eq - start) / start * 100).quantize(Q2)) if start else "0",
            "completed_cycles": s.completed_cycles,
            "buy_count": s.buys,
            "sell_count": s.sells,
            "turnover": str(
                sum((lot.buy_funds for lot in s.lots), ZERO)
                + sum((c.buy_funds + c.sell_funds for c in self.cycles), ZERO)
            ),
            "eth_inventory_qty": str(s.inventory_qty),
            "eth_inventory_value": str(inv_v),
            "eth_inventory_cost": str(s.inventory_cost),
            "krw_inventory": str(s.cash),
            "avg_cycle_profit": str(round(statistics.fmean(nets), 2)) if nets else None,
            "median_cycle_profit": str(round(statistics.median(nets), 2)) if nets else None,
            "avg_cycle_duration_sec": round(statistics.fmean(durs), 1) if durs else None,
            "best_cycle": str(max(nets)) if nets else None,
            "worst_cycle": str(min(nets)) if nets else None,
            "today_pnl": str((eq - s.day_start_equity).quantize(Q2)) if s.day_start_equity else "0",
            "today_buys": s.day_buys,
            "today_sells": s.day_sells,
            "lots": len(s.lots),
            "status": self.status.value,
            "current_unit": str(self.current_unit),
            "compounding_capital": str(self.realized_capital()),
            "sizing_epoch": s.sizing_epoch,
        }
