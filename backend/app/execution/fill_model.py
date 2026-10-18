"""Fill simulation shared by BACKTEST and PAPER.

Assumptions (documented, configurable):
- Market BUY (`ord_type=price`): `krw_amount` buys BTC at ref*(1+slip); fee = krw_amount*fee_rate
  is charged IN ADDITION (Upbit locks price + reserved_fee). Sizing must leave room for the fee.
- Market SELL (`ord_type=market`): proceeds = volume*ref*(1-slip); fee = proceeds*fee_rate.

Intrabar exit ambiguity (REQUIRED POLICY): when a bar touches both the stop and a target, the
order of events is unknowable -> STOP LOSS WINS. If the bar OPENS beyond the stop (gap), the fill
is the open (worse than the stop), never the stop price. See tests/unit/test_fill_model.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.types import Candle, Fill
from app.portfolio.accounting import q_btc, q_krw

BPS = Decimal(10000)


class ZeroVolumeError(ValueError):
    """Order amount is below one unit of quantity precision (1e-8 BTC) — cannot fill."""


class ExitKind(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"


@dataclass(frozen=True, slots=True)
class FillParams:
    fee_rate: Decimal = Decimal("0.0005")
    slippage_bps: Decimal = Decimal(3)

    @property
    def slip(self) -> Decimal:
        return self.slippage_bps / BPS


@dataclass(frozen=True, slots=True)
class IntrabarExit:
    kind: ExitKind
    price: Decimal  # reference price BEFORE slippage
    reason: str


class FillModel:
    def __init__(self, params: FillParams) -> None:
        self.p = params

    def max_buy_krw(self, krw_available: Decimal) -> Decimal:
        """Largest `krw_amount` such that amount + fee <= available."""
        return q_krw(krw_available / (1 + self.p.fee_rate))

    def market_buy(
        self, client_id: str, krw_amount: Decimal, ref_price: Decimal, ts: datetime
    ) -> Fill:
        price = ref_price * (1 + self.p.slip)
        volume = q_btc(krw_amount / price)
        if volume <= 0:
            raise ZeroVolumeError(f"{krw_amount} KRW buys < 1e-8 BTC at {price}")
        funds = q_krw(volume * price)
        fee = q_krw(funds * self.p.fee_rate)
        return Fill(
            order_client_id=client_id,
            price=price,
            volume=volume,
            funds=funds,
            fee=fee,
            timestamp=ts,
        )

    def market_sell(
        self, client_id: str, volume: Decimal, ref_price: Decimal, ts: datetime
    ) -> Fill:
        price = ref_price * (1 - self.p.slip)
        funds = q_krw(volume * price)
        fee = q_krw(funds * self.p.fee_rate)
        return Fill(
            order_client_id=client_id,
            price=price,
            volume=volume,
            funds=funds,
            fee=fee,
            timestamp=ts,
        )

    @staticmethod
    def resolve_intrabar_exit(
        bar: Candle, stop_price: Decimal | None, target_price: Decimal | None
    ) -> IntrabarExit | None:
        """Conservative exit resolution for a long position over one bar.

        Priority: STOP over TARGET whenever both are touched. Gap-through -> fill at open.
        """
        stop_hit = stop_price is not None and bar.low <= stop_price
        target_hit = target_price is not None and bar.high >= target_price
        if stop_hit:
            assert stop_price is not None
            px = min(bar.open, stop_price)  # gap down through the stop -> open
            reason = "STOP_AND_TARGET_SAME_BAR->STOP_PRIORITY" if target_hit else "STOP"
            return IntrabarExit(ExitKind.STOP, px, reason)
        if target_hit:
            assert target_price is not None
            # conservative: never assume a better-than-target fill even if the bar gapped up
            return IntrabarExit(ExitKind.TARGET, target_price, "TARGET")
        return None
