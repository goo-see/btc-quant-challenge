"""Execution measurement — wired into TraderService._execute (PAPER and LIVE) and persisted in
the `execution_metrics` table (one row per filled order, idempotent on client_order_id).

For every filled entry we want to keep, per order:
  signal_price               strategy-TF close the signal was computed on
  actual_fill_price          volume-weighted fill price from the exchange trades
  slippage_bps               (fill - signal) / signal * 1e4  (positive = paid more on a BUY)
  initial_stop               stop carried on the signal (= signal_price - 3*ATR for D)
  stop_distance_from_signal  (signal_price - initial_stop) / signal_price
  stop_distance_from_fill    (fill_price   - initial_stop) / fill_price
`build_execution_record` produces the persisted row: signal/send/fill prices, slippage vs signal
and vs the reference price at send, fee, quantity, ATR + stop distances (BUY) and exit reason /
trigger / realized PnL / return / holding time (SELL). /api/executions exposes the rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.types import Fill, OrderRecord, Side, Signal

BPS = Decimal(10000)


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    side: str
    signal_price: Decimal
    actual_fill_price: Decimal
    slippage_bps: Decimal
    initial_stop: Decimal | None
    stop_distance_from_signal: Decimal | None
    stop_distance_from_fill: Decimal | None
    fill_volume: Decimal
    fill_count: int

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def vwap(fills: list[Fill]) -> Decimal:
    vol = sum((f.volume for f in fills), Decimal(0))
    if vol <= 0:
        raise ValueError("no filled volume")
    return sum((f.funds for f in fills), Decimal(0)) / vol


def measure(
    *, side: Side, signal_price: Decimal, fills: list[Fill], initial_stop: Decimal | None
) -> ExecutionMetrics:
    fill_px = vwap(fills)
    sign = Decimal(1) if side is Side.BID else Decimal(-1)  # adverse slippage is positive
    slip = (fill_px - signal_price) / signal_price * BPS * sign
    d_sig = ((signal_price - initial_stop) / signal_price) if initial_stop is not None else None
    d_fill = ((fill_px - initial_stop) / fill_px) if initial_stop is not None else None
    return ExecutionMetrics(
        side=side.value,
        signal_price=signal_price,
        actual_fill_price=fill_px,
        slippage_bps=slip.quantize(Decimal("0.01")),
        initial_stop=initial_stop,
        stop_distance_from_signal=d_sig.quantize(Decimal("0.000001"))
        if d_sig is not None
        else None,
        stop_distance_from_fill=d_fill.quantize(Decimal("0.000001"))
        if d_fill is not None
        else None,
        fill_volume=sum((f.volume for f in fills), Decimal(0)),
        fill_count=len(fills),
    )


def _q(x: Decimal | None, places: str = "0.01") -> Decimal | None:
    return None if x is None else x.quantize(Decimal(places))


def build_execution_record(
    *,
    rec: OrderRecord,
    sig: Signal,
    strategy_version: str,
    reference_price_at_send: Decimal | None,
    atr_trail_mult: float,
    realized_pnl: Decimal | None = None,
    entry_price: Decimal | None = None,
    entry_time: datetime | None = None,
) -> dict[str, Any]:
    """Persistable execution-quality row for a FILLED (or partially filled) order."""
    fills = list(rec.fills)
    fill_px = vwap(fills)
    ind = sig.indicators
    signal_price = (
        Decimal(str(ind["close"]))
        if "close" in ind
        else (Decimal(str(ind["tick_price"])) if "tick_price" in ind else reference_price_at_send)
    )
    side = rec.side
    sign = Decimal(1) if side is Side.BID else Decimal(-1)
    slip_sig = ((fill_px - signal_price) / signal_price * BPS * sign) if signal_price else None
    slip_ref = (
        ((fill_px - reference_price_at_send) / reference_price_at_send * BPS * sign)
        if reference_price_at_send
        else None
    )
    qty = sum((f.volume for f in fills), Decimal(0))
    funds = sum((f.funds for f in fills), Decimal(0))
    fee = sum((f.fee for f in fills), Decimal(0)) or rec.paid_fee
    filled_at = fills[-1].timestamp
    row: dict[str, Any] = {
        "mode": rec.mode.value,
        "strategy_version": strategy_version,
        "client_order_id": rec.client_order_id,
        "order_id": rec.db_id,
        "signal_id": rec.signal_id,
        "side": "BUY" if side is Side.BID else "SELL",
        "signal_time": sig.timestamp,
        "signal_price": signal_price,
        "order_sent_at": rec.submitted_at,
        "reference_price_at_send": reference_price_at_send,
        "actual_fill_price": _q(fill_px, "0.00000001"),
        "slippage_bps": _q(slip_sig),
        "slippage_vs_reference_bps": _q(slip_ref),
        "fee": fee,
        "quantity": qty,
        "funds": funds,
        "fill_count": len(fills),
        "atr": Decimal(str(ind["atr"])) if ind.get("atr") is not None else None,
        "initial_stop": None,
        "stop_distance_from_signal": None,
        "stop_distance_from_fill": None,
        "exit_reason": None,
        "exit_trigger_price": None,
        "realized_pnl": None,
        "return_pct": None,
        "holding_sec": None,
        "entry_time": entry_time,
        "filled_at": filled_at,
        "created_at": datetime.now(UTC),
        "extra": {"signal_reason": sig.reason, "order_state": rec.state.value},
    }
    if side is Side.BID:
        stop = sig.stop_price
        row["initial_stop"] = stop
        if stop is not None and signal_price:
            row["stop_distance_from_signal"] = _q((signal_price - stop) / signal_price, "0.000001")
            row["stop_distance_from_fill"] = _q((fill_px - stop) / fill_px, "0.000001")
    else:
        row["exit_reason"] = sig.reason
        # trigger: the stop for STOP exits, else the channel/trend level carried on the signal
        trigger = (
            sig.stop_price
            if sig.reason.startswith("STOP")
            else (
                Decimal(str(ind["channel_lower"]))
                if (sig.reason.startswith("CHANNEL_EXIT") and "channel_lower" in ind)
                else (
                    Decimal(str(ind["trend_ema"]))
                    if (sig.reason.startswith("TREND_LOSS") and "trend_ema" in ind)
                    else signal_price
                )
            )
        )
        row["exit_trigger_price"] = trigger
        row["realized_pnl"] = realized_pnl
        if realized_pnl is not None and entry_price and qty:
            row["return_pct"] = _q(realized_pnl / (entry_price * qty) * 100, "0.0001")
        if entry_time is not None:
            row["holding_sec"] = int((filled_at - entry_time).total_seconds())
        row["extra"]["atr_trail_mult"] = atr_trail_mult
    return row
