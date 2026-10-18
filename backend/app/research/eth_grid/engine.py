"""Tick-driven ETH grid engine.

Grid: lattice of buy levels L_k = anchor·(1+gap)^k; a unit (₩unit_krw) is bought when the price
crosses DOWN through an empty level; every held unit carries its own sell target
entry·(1+gap) and is sold when the price crosses UP through it (a unit bought on the current
observation can never sell on the same observation). Seed inventory (₩seed_krw at the start price)
is laddered: unit j sells at anchor·(1+gap)^j, j = 1..n.
Guards (pre-registered, fixed): max ETH inventory ₩40,000 and min cash ₩10,000 (buy blocked),
daily (KST) sleeve loss ≤ −5% → buys paused for the day, sleeve MDD ≥ 12% → HALT (no buys, sells
continue), daily close < EMA100 → buys paused (sells continue). Re-centering (price outside the
±levels range) moves only the buy lattice; units, PnL and inventory are preserved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class GridParams:
    key: str
    description: str
    gap_pct: Decimal | None  # fixed gap; None -> ATR adaptive
    atr_min_pct: Decimal = Decimal("0.0075")
    atr_max_pct: Decimal = Decimal("0.02")
    atr_mult: Decimal = Decimal(1)  # gap = clamp(1h ATR% * mult)
    budget_krw: Decimal = Decimal(50000)
    unit_krw: Decimal = Decimal(5000)
    seed_krw: Decimal = Decimal(25000)
    max_inventory_krw: Decimal = Decimal(40000)
    min_cash_krw: Decimal = Decimal(10000)
    levels_each_side: int = 10
    fee_rate: Decimal = Decimal("0.0005")
    slip: Decimal = Decimal("0.0005")  # 3 bps slippage + 2 bps half-spread, each side
    daily_loss_pause_pct: Decimal = Decimal("0.05")
    mdd_halt_pct: Decimal = Decimal("0.12")
    ema_period: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def grid_candidates() -> list[GridParams]:
    return [
        GridParams("G0", "fixed 0.75% gap", Decimal("0.0075")),
        GridParams("G1", "fixed 1.0% gap", Decimal("0.01")),
        GridParams("G2", "fixed 1.5% gap", Decimal("0.015")),
        GridParams("G3", "ATR-adaptive gap = clamp(1h ATR14 %, 0.75%, 2.0%)", None),
    ]


@dataclass(slots=True)
class Unit:
    uid: int
    entry_time: datetime
    entry_price: Decimal  # effective (slip applied)
    quantity: Decimal
    cost: Decimal  # funds + fee
    target: Decimal  # sell trigger price
    level: int | None  # lattice level it was bought at (None for seed)
    seed: bool = False


@dataclass(slots=True)
class Cycle:
    uid: int
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross: Decimal
    fees: Decimal
    net: Decimal
    seed: bool

    @property
    def duration_sec(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds()


@dataclass(slots=True)
class GridState:
    cash: Decimal
    units: list[Unit] = field(default_factory=list)
    anchor: Decimal = ZERO
    gap: Decimal = ZERO
    next_uid: int = 1
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    hwm: Decimal = ZERO
    mdd: Decimal = ZERO
    halted: bool = False
    halt_reason: str | None = None
    day_key: str = ""
    day_start_equity: Decimal = ZERO
    day_paused: bool = False
    trend_ok: bool = True
    ema: Decimal | None = None
    last_price: Decimal = ZERO
    last_ts: datetime | None = None
    recenters: int = 0
    buys: int = 0
    sells: int = 0
    units_bought_now: set[int] = field(default_factory=set)

    @property
    def inventory_qty(self) -> Decimal:
        return sum((u.quantity for u in self.units), ZERO)

    def inventory_value(self, price: Decimal) -> Decimal:
        return (self.inventory_qty * price).quantize(Q2)

    def equity(self, price: Decimal) -> Decimal:
        return (self.cash + self.inventory_value(price)).quantize(Q2)


@dataclass(slots=True)
class GridEvent:
    ts: datetime
    kind: str  # SEED | BUY | SELL | RECENTER | PAUSE | RESUME | HALT | GUARD_ON | GUARD_OFF
    detail: dict[str, Any] = field(default_factory=dict)


class GridEngine:
    def __init__(self, p: GridParams) -> None:
        self.p = p
        self.s = GridState(cash=p.budget_krw)
        self.cycles: list[Cycle] = []
        self.events: list[GridEvent] = []
        self.daily_equity: list[tuple[str, Decimal]] = []  # (KST day, equity at day end)
        self._daily_trades: dict[str, int] = {}
        self._level_blocked: set[int] = set()

    # ------------------------------------------------------------------ helpers
    def _emit(self, ts: datetime, kind: str, **detail: Any) -> None:
        self.events.append(
            GridEvent(
                ts, kind, {k: (str(v) if isinstance(v, Decimal) else v) for k, v in detail.items()}
            )
        )

    def level_price(self, k: int) -> Decimal:
        return (self.s.anchor * (1 + self.s.gap) ** k).quantize(Decimal(1))

    def set_gap(self, gap: Decimal) -> None:
        self.s.gap = (
            max(self.p.atr_min_pct, min(self.p.atr_max_pct, gap))
            if self.p.gap_pct is None
            else self.p.gap_pct
        )

    def _recenter(self, ts: datetime, price: Decimal, reason: str) -> None:
        self.s.anchor = price
        self._level_blocked.clear()
        self.s.recenters += 1
        self._emit(
            ts, "RECENTER", anchor=price, gap=self.s.gap, units=len(self.s.units), reason=reason
        )

    def buys_allowed(self, price: Decimal) -> tuple[bool, str]:
        if self.s.halted:
            return False, "HALTED"
        if not self.s.trend_ok:
            return False, "DOWNTREND_GUARD"
        if self.s.day_paused:
            return False, "DAILY_LOSS_PAUSE"
        if self.s.inventory_value(price) + self.p.unit_krw > self.p.max_inventory_krw:
            return False, "MAX_INVENTORY"
        if self.s.cash - self.p.unit_krw * (1 + self.p.fee_rate) < self.p.min_cash_krw:
            return False, "MIN_CASH_RESERVE"
        return True, "OK"

    # ------------------------------------------------------------------ fills
    def _buy(
        self,
        ts: datetime,
        ref: Decimal,
        level: int | None,
        *,
        seed: bool = False,
        krw: Decimal | None = None,
    ) -> Unit:
        krw = krw or self.p.unit_krw
        px = ref * (1 + self.p.slip)
        qty = (krw / px).quantize(Q8, rounding=ROUND_DOWN)
        funds = (qty * px).quantize(Q2)
        fee = (funds * self.p.fee_rate).quantize(Q2)
        self.s.cash -= funds + fee
        self.s.fees += fee
        u = Unit(
            self.s.next_uid,
            ts,
            px,
            qty,
            funds + fee,
            (ref * (1 + self.s.gap)).quantize(Decimal(1)),
            level,
            seed,
        )
        self.s.next_uid += 1
        self.s.units.append(u)
        self.s.buys += 1
        self.s.units_bought_now.add(u.uid)
        self._count_trade(ts)
        self._emit(
            ts,
            "SEED" if seed else "BUY",
            uid=u.uid,
            ref=ref,
            fill=px,
            qty=qty,
            level=level,
            target=u.target,
            cash=self.s.cash,
        )
        return u

    def _sell(self, ts: datetime, u: Unit, ref: Decimal, reason: str = "GRID_TARGET") -> Cycle:
        px = ref * (1 - self.p.slip)
        funds = (u.quantity * px).quantize(Q2)
        fee = (funds * self.p.fee_rate).quantize(Q2)
        self.s.cash += funds - fee
        self.s.fees += fee
        gross = (funds - (u.cost - (u.cost - u.quantity * u.entry_price))).quantize(
            Q2
        )  # vs entry funds (ex fee)
        net = (funds - fee - u.cost).quantize(Q2)
        self.s.realized += net
        self.s.units.remove(u)
        if u.level is not None:
            self._level_blocked.discard(u.level)
        self.s.sells += 1
        self._count_trade(ts)
        c = Cycle(
            u.uid,
            u.entry_time,
            ts,
            u.entry_price,
            px,
            u.quantity,
            gross,
            fee + (u.cost - u.quantity * u.entry_price),
            net,
            u.seed,
        )
        self.cycles.append(c)
        self._emit(
            ts,
            "SELL",
            uid=u.uid,
            ref=ref,
            fill=px,
            qty=u.quantity,
            net=net,
            reason=reason,
            cash=self.s.cash,
        )
        return c

    def _count_trade(self, ts: datetime) -> None:
        k = ts.astimezone(KST).strftime("%Y-%m-%d")
        self._daily_trades[k] = self._daily_trades.get(k, 0) + 1

    # ------------------------------------------------------------------ lifecycle
    def seed(self, ts: datetime, price: Decimal, gap_hint: Decimal | None = None) -> None:
        """Start: anchor at the current price, buy the seed inventory (laddered sell targets)."""
        self.set_gap(gap_hint or self.p.atr_min_pct)
        self.s.anchor = price
        n = int(self.p.seed_krw / self.p.unit_krw)
        for j in range(1, n + 1):
            u = self._buy(ts, price, None, seed=True)
            u.target = (price * (1 + self.s.gap) ** j).quantize(Decimal(1))
        self.s.units_bought_now.clear()
        self.s.last_price, self.s.last_ts = price, ts
        self.s.hwm = self.s.equity(price)
        self.s.day_key = ts.astimezone(KST).strftime("%Y-%m-%d")
        self.s.day_start_equity = self.s.equity(price)
        self._emit(ts, "START", anchor=price, gap=self.s.gap, units=n, cash=self.s.cash)

    def set_trend(self, ts: datetime, daily_close: Decimal, ema: Decimal | None) -> None:
        self.s.ema = ema
        ok = ema is None or daily_close > ema
        if ok != self.s.trend_ok:
            self._emit(ts, "GUARD_OFF" if ok else "GUARD_ON", close=daily_close, ema=ema)
        self.s.trend_ok = ok

    def _roll_day(self, ts: datetime, price: Decimal) -> None:
        k = ts.astimezone(KST).strftime("%Y-%m-%d")
        if k != self.s.day_key:
            if self.s.day_key:
                self.daily_equity.append((self.s.day_key, self.s.equity(self.s.last_price)))
            self.s.day_key = k
            self.s.day_start_equity = self.s.equity(price)
            if self.s.day_paused:
                self._emit(ts, "RESUME", reason="new_day")
            self.s.day_paused = False

    def on_price(self, ts: datetime, price: Decimal, *, new_observation: bool = True) -> None:
        """One market observation. Order: sells (targets crossed up) -> risk/MDD bookkeeping ->
        buys (levels crossed down). Units bought on this observation cannot sell on it
        (`new_observation=False` keeps the same-observation set across a bar's internal ticks)."""
        s = self.s
        if s.anchor == ZERO:
            self.seed(ts, price)
            return
        self._roll_day(ts, price)
        prev = s.last_price or price
        if new_observation:
            s.units_bought_now.clear()
        # 1) sells
        for u in sorted(
            [u for u in s.units if price >= u.target and u.uid not in s.units_bought_now],
            key=lambda u: u.target,
        ):
            self._sell(ts, u, max(u.target, min(price, u.target)) if prev <= u.target else price)
        # 2) bookkeeping: equity, HWM, MDD, daily loss, halt
        eq = s.equity(price)
        s.hwm = max(s.hwm, eq)
        dd = (s.hwm - eq) / s.hwm if s.hwm > 0 else ZERO
        s.mdd = max(s.mdd, dd)
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
        # 3) re-center when outside the lattice
        if price > self.level_price(self.p.levels_each_side) or price < self.level_price(
            -self.p.levels_each_side
        ):
            self._recenter(ts, price, "OUT_OF_RANGE")
        # 4) buys: levels crossed downward between prev and price (strictly below the anchor side
        #    is not required: any empty level crossed from above is a buy)
        if price < prev:
            for k in range(self.p.levels_each_side, -self.p.levels_each_side - 1, -1):
                lp = self.level_price(k)
                if lp >= prev or lp < price or k in self._level_blocked or lp >= s.anchor:
                    continue
                if any(abs(u.entry_price / lp - 1) < s.gap / 2 for u in s.units):
                    continue  # a unit already sits at this level
                ok, why = self.buys_allowed(lp)
                if not ok:
                    self._emit(ts, "BUY_BLOCKED", level=k, price=lp, reason=why)
                    break
                self._buy(ts, lp, k)
                self._level_blocked.add(k)
        s.last_price, s.last_ts = price, ts

    def on_bar(
        self, ts_open: datetime, o: Decimal, h: Decimal, lo: Decimal, c: Decimal, tf_minutes: int
    ) -> None:
        """Pessimistic bar feed: open -> (if red bar) high then low -> close; (if green) low then high -> close.
        A unit bought at the low cannot sell at the high of the same bar (units_bought_now rule)."""
        end = ts_open + timedelta(minutes=tf_minutes)
        self.on_price(ts_open, o)  # new observation: clears the same-bar set
        if c >= o:
            self.on_price(ts_open + timedelta(seconds=tf_minutes * 20), lo, new_observation=False)
            self.on_price(ts_open + timedelta(seconds=tf_minutes * 40), h, new_observation=False)
        else:
            self.on_price(ts_open + timedelta(seconds=tf_minutes * 20), h, new_observation=False)
            self.on_price(ts_open + timedelta(seconds=tf_minutes * 40), lo, new_observation=False)
        self.on_price(end - timedelta(seconds=1), c, new_observation=False)

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        s = self.s

        def d(v: Any) -> Any:
            return (
                str(v)
                if isinstance(v, Decimal)
                else v.isoformat()
                if isinstance(v, datetime)
                else v
            )

        return {
            "state": {
                k: d(v) for k, v in asdict(s).items() if k not in ("units", "units_bought_now")
            },
            "units": [{k: d(v) for k, v in asdict(u).items()} for u in s.units],
            "level_blocked": sorted(self._level_blocked),
            "daily_trades": dict(self._daily_trades),
            "daily_equity": [(k, str(v)) for k, v in self.daily_equity],
            "cycles": len(self.cycles),
            "cycle_stats": {
                "net_sum": str(sum((c.net for c in self.cycles), ZERO)),
                "wins": sum(1 for c in self.cycles if c.net > 0),
            },
        }

    @classmethod
    def from_json(cls, p: GridParams, d: dict[str, Any]) -> GridEngine:
        eng = cls(p)
        st = d["state"]
        dec = {
            "cash",
            "anchor",
            "gap",
            "realized",
            "fees",
            "hwm",
            "mdd",
            "day_start_equity",
            "last_price",
        }
        for k, v in st.items():
            if k in dec:
                setattr(eng.s, k, Decimal(str(v)))
            elif k == "ema":
                eng.s.ema = Decimal(str(v)) if v is not None else None
            elif k == "last_ts":
                eng.s.last_ts = datetime.fromisoformat(v) if v else None
            elif hasattr(eng.s, k):
                setattr(eng.s, k, v)
        eng.s.units = [
            Unit(
                int(u["uid"]),
                datetime.fromisoformat(u["entry_time"]),
                Decimal(u["entry_price"]),
                Decimal(u["quantity"]),
                Decimal(u["cost"]),
                Decimal(u["target"]),
                u.get("level"),
                bool(u.get("seed")),
            )
            for u in d.get("units", [])
        ]
        eng._level_blocked = set(d.get("level_blocked", []))
        eng._daily_trades = dict(d.get("daily_trades", {}))
        eng.daily_equity = [(k, Decimal(v)) for k, v in d.get("daily_equity", [])]
        return eng

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        s, px = self.s, self.s.last_price
        return {
            "cash": str(s.cash),
            "inventory_qty": str(s.inventory_qty),
            "inventory_value": str(s.inventory_value(px)) if px else "0",
            "equity": str(s.equity(px)) if px else str(s.cash),
            "realized": str(s.realized),
            "fees": str(s.fees),
            "unrealized": str(
                (s.inventory_value(px) - sum((u.cost for u in s.units), ZERO)).quantize(Q2)
            )
            if px
            else "0",
            "anchor": str(s.anchor),
            "gap": str(s.gap),
            "units": len(s.units),
            "buys": s.buys,
            "sells": s.sells,
            "cycles": len(self.cycles),
            "hwm": str(s.hwm),
            "mdd_pct": str((s.mdd * 100).quantize(Q2)),
            "halted": s.halted,
            "halt_reason": s.halt_reason,
            "day_paused": s.day_paused,
            "trend_ok": s.trend_ok,
            "ema": str(s.ema) if s.ema is not None else None,
            "next_buy": str(
                min(
                    (
                        self.level_price(k)
                        for k in range(-self.p.levels_each_side, 0)
                        if self.level_price(k) < px and k not in self._level_blocked
                    ),
                    default=ZERO,
                )
            )
            if px
            else None,
            "next_sell": str(min((u.target for u in s.units), default=ZERO)),
            "last_price": str(px),
            "last_ts": s.last_ts.isoformat() if s.last_ts else None,
            "recenters": s.recenters,
        }
