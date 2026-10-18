"""Pure shadow-book logic (no I/O): entry, tick processing, stop sources, break-even, exits.

Event ordering per tick (never look-ahead, never optimistic):
  1. stop check against the CURRENT effective stop (a tick at/below it exits at that tick's price)
  2. excursion update (highest / lowest) and, for S1, +1R detection -> break-even activation
     (the BE stop is in force from the NEXT tick on)
Bar catch-up (after a restart, from closed 1m candles) is pessimistic: the bar LOW is processed
before the bar HIGH, so a bar that both touches +1R and the stop exits without BE protection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_UP, Decimal
from typing import Any, Literal

from app.core.types import Candle

StopSource = Literal["INITIAL", "TRAILING", "BREAK_EVEN"]

FEE = Decimal("0.0005")
SLIP = Decimal("0.0003")  # 3 bps, same as the backtests and the PAPER fill model
Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")


def break_even_stop(entry: Decimal, fee: Decimal = FEE, slip: Decimal = SLIP) -> Decimal:
    """Smallest stop price whose market-sell fill (stop*(1-slip), fee on proceeds) covers the
    entry cost incl. buy fee: net PnL >= 0  <=>  stop >= entry*(1+fee) / ((1-slip)*(1-fee)).
    Same formula as the historical E1 research (lock_r = 0)."""
    return (entry * (1 + fee) / ((1 - slip) * (1 - fee))).quantize(Decimal(1), rounding=ROUND_UP)


@dataclass(slots=True)
class Fill:
    ts: datetime
    price: Decimal  # effective (slippage applied)
    ref_price: Decimal  # market observation used
    quantity: Decimal
    funds: Decimal
    fee: Decimal


def virtual_buy(
    ts: datetime, ref_price: Decimal, krw_amount: Decimal, fee: Decimal = FEE, slip: Decimal = SLIP
) -> Fill:
    price = ref_price * (1 + slip)
    qty = (krw_amount / price).quantize(Q8)
    funds = (qty * price).quantize(Q2)
    return Fill(ts, price, ref_price, qty, funds, (funds * fee).quantize(Q2))


def virtual_sell(
    ts: datetime, ref_price: Decimal, qty: Decimal, fee: Decimal = FEE, slip: Decimal = SLIP
) -> Fill:
    price = ref_price * (1 - slip)
    funds = (qty * price).quantize(Q2)
    return Fill(ts, price, ref_price, qty, funds, (funds * fee).quantize(Q2))


@dataclass(slots=True)
class Event:
    ts: datetime
    kind: str  # ENTRY | STOP_UPDATE | BE_ACTIVATED | EXIT | CATCHUP_BAR
    book: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BookState:
    """Everything needed to restore a shadow position exactly after a restart."""

    book: str  # S0 | S1
    entry_id: int
    entry_time: datetime
    entry_price: Decimal  # virtual fill (slippage applied)
    quantity: Decimal
    entry_fee: Decimal
    initial_stop: Decimal
    r_unit: Decimal
    be_trigger: Decimal
    be_stop: Decimal
    trailing_stop: Decimal | None = None  # mirrored from the LIVE D0 ratchet (S0 and S1 alike)
    be_activated: bool = False
    be_activated_at: datetime | None = None
    be_trigger_price: Decimal | None = None
    highest: Decimal = Decimal(0)
    lowest: Decimal = Decimal(0)
    last_price: Decimal = Decimal(0)
    last_tick_at: datetime | None = None
    exit: Fill | None = None
    exit_reason: str | None = None
    exit_source: str | None = None

    @property
    def open(self) -> bool:
        return self.exit is None

    @property
    def uses_be(self) -> bool:
        return self.book == "S1"

    def effective_stop(self) -> tuple[Decimal, StopSource]:
        best, src = self.initial_stop, "INITIAL"
        if self.trailing_stop is not None and self.trailing_stop > best:
            best, src = self.trailing_stop, "TRAILING"
        if self.uses_be and self.be_activated and self.be_stop > best:
            best, src = self.be_stop, "BREAK_EVEN"
        return best, src  # type: ignore[return-value]

    def net_pnl(self, price: Decimal | None = None) -> Decimal:
        if self.exit is not None:
            return (
                (self.exit.funds - self.exit.fee)
                - (self.quantity * self.entry_price).quantize(Q2)
                - self.entry_fee
            )
        px = price if price is not None else self.last_price
        return ((px - self.entry_price) * self.quantity).quantize(Q2) - self.entry_fee

    def mfe_r(self) -> float:
        return (
            float((self.highest - self.entry_price) / self.r_unit)
            if self.r_unit > 0 and self.highest
            else 0.0
        )

    def mae_r(self) -> float:
        return (
            float((self.lowest - self.entry_price) / self.r_unit)
            if self.r_unit > 0 and self.lowest
            else 0.0
        )

    # ------------------------------------------------------------------ mutations
    def apply_trailing(self, live_stop: Decimal | None, ts: datetime) -> Event | None:
        """Mirror the LIVE D0 trailing ratchet (never lowers anything)."""
        if live_stop is None or not self.open:
            return None
        if self.trailing_stop is None or live_stop > self.trailing_stop:
            self.trailing_stop = live_stop
            eff, src = self.effective_stop()
            return Event(
                ts,
                "STOP_UPDATE",
                self.book,
                {"trailing_stop": str(live_stop), "effective_stop": str(eff), "source": src},
            )
        return None

    def on_tick(self, ts: datetime, price: Decimal, *, seq: int = 0) -> list[Event]:
        """Tick arrival order: stop check first, then excursion / break-even detection."""
        if not self.open:
            return []
        out: list[Event] = []
        self.last_price, self.last_tick_at = price, ts
        stop, src = self.effective_stop()
        if price <= stop:
            self.exit = virtual_sell(ts, price, self.quantity)
            self.exit_reason = f"STOP@{stop:.0f} (tick {price:.0f})"
            self.exit_source = src
            out.append(
                Event(
                    ts,
                    "EXIT",
                    self.book,
                    {
                        "reason": self.exit_reason,
                        "source": src,
                        "stop": str(stop),
                        "tick": str(price),
                        "seq": seq,
                        "net_pnl": str(self.net_pnl()),
                    },
                )
            )
            return out
        if not self.highest or price > self.highest:
            self.highest = price
        if not self.lowest or price < self.lowest:
            self.lowest = price
        if self.uses_be and not self.be_activated and price >= self.be_trigger:
            self.be_activated, self.be_activated_at, self.be_trigger_price = True, ts, price
            eff, src2 = self.effective_stop()
            out.append(
                Event(
                    ts,
                    "BE_ACTIVATED",
                    self.book,
                    {
                        "trigger": str(self.be_trigger),
                        "tick": str(price),
                        "be_stop": str(self.be_stop),
                        "effective_stop": str(eff),
                        "source": src2,
                        "seq": seq,
                    },
                )
            )
        return out

    def on_catchup_bar(self, bar: Candle) -> list[Event]:
        """Closed 1m bar after a restart: pessimistic LOW-then-HIGH ordering."""
        if not self.open:
            return []
        evs = self.on_tick(bar.open_time, bar.low, seq=-1)
        if self.open:
            evs += self.on_tick(bar.close_time, bar.high, seq=-2)
            if self.open:
                self.last_price = bar.close
        for e in evs:
            e.kind = e.kind if e.kind != "EXIT" else "EXIT"
            e.detail["catchup_bar"] = bar.open_time.isoformat()
        return evs

    def on_live_sell(self, ts: datetime, ref_price: Decimal, reason: str) -> Event | None:
        """The LIVE D0 decided to SELL (channel exit / trend loss / stop): S0 mirrors it; S1 too if
        still open (both exit at the same market observation)."""
        if not self.open:
            return None
        self.exit = virtual_sell(ts, ref_price, self.quantity)
        self.exit_reason = reason
        self.exit_source = "LIVE_SIGNAL"
        return Event(
            ts,
            "EXIT",
            self.book,
            {
                "reason": reason,
                "source": "LIVE_SIGNAL",
                "ref": str(ref_price),
                "net_pnl": str(self.net_pnl()),
            },
        )

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        out: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, Decimal):
                out[k] = str(v)
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            elif k == "exit" and v is not None:
                out[k] = {
                    kk: (
                        str(vv)
                        if isinstance(vv, Decimal)
                        else vv.isoformat()
                        if isinstance(vv, datetime)
                        else vv
                    )
                    for kk, vv in v.items()
                }
            else:
                out[k] = v
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> BookState:
        def dec(k: str) -> Decimal | None:
            return Decimal(str(d[k])) if d.get(k) is not None else None

        def dt(k: str) -> datetime | None:
            return datetime.fromisoformat(d[k]) if d.get(k) else None

        ex = d.get("exit")
        st = cls(
            book=d["book"],
            entry_id=int(d["entry_id"]),
            entry_time=dt("entry_time") or datetime.min,
            entry_price=dec("entry_price") or Decimal(0),
            quantity=dec("quantity") or Decimal(0),
            entry_fee=dec("entry_fee") or Decimal(0),
            initial_stop=dec("initial_stop") or Decimal(0),
            r_unit=dec("r_unit") or Decimal(0),
            be_trigger=dec("be_trigger") or Decimal(0),
            be_stop=dec("be_stop") or Decimal(0),
            trailing_stop=dec("trailing_stop"),
            be_activated=bool(d.get("be_activated")),
            be_activated_at=dt("be_activated_at"),
            be_trigger_price=dec("be_trigger_price"),
            highest=dec("highest") or Decimal(0),
            lowest=dec("lowest") or Decimal(0),
            last_price=dec("last_price") or Decimal(0),
            last_tick_at=dt("last_tick_at"),
            exit_reason=d.get("exit_reason"),
            exit_source=d.get("exit_source"),
        )
        if ex:
            st.exit = Fill(
                datetime.fromisoformat(ex["ts"]),
                Decimal(ex["price"]),
                Decimal(ex["ref_price"]),
                Decimal(ex["quantity"]),
                Decimal(ex["funds"]),
                Decimal(ex["fee"]),
            )
        return st


def open_pair(entry_id: int, fill: Fill, initial_stop: Decimal) -> tuple[BookState, BookState]:
    """S0 and S1 start from the SAME virtual fill; R and the BE levels are fixed at entry."""
    r = fill.price - initial_stop
    be_trigger = fill.price + r
    be_stop = break_even_stop(fill.price)

    def mk(b: str) -> BookState:
        return BookState(
            b,
            entry_id,
            fill.ts,
            fill.price,
            fill.quantity,
            fill.fee,
            initial_stop,
            r,
            be_trigger,
            be_stop,
            highest=fill.price,
            lowest=fill.price,
            last_price=fill.price,
            last_tick_at=fill.ts,
        )

    return mk("S0"), mk("S1")
