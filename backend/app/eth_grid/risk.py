"""Risk layer for the ETH grid LIVE EXPERIMENT (Strategy -> GridRisk -> OrderManager -> Exchange).

Strategy guards (inventory cap, cash reserve, downtrend pause, daily-loss pause, MDD halt) live in
the engine (`buys_allowed`) exactly as in research; this layer adds execution safety: FROZEN state,
in-flight/unknown orders, stale market data, Upbit minimum total, exchange balance sufficiency and
a runaway order-rate guard that is never a tuning knob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.config.settings import EthGridConfig
from app.core.types import RiskDecision, Side
from app.eth_grid.live_engine import Intent, LiveGridEngine
from app.exchange.upbit.tick_size import meets_min_total

ONE = Decimal(1)


@dataclass(slots=True)
class GridRiskContext:
    now: datetime
    price: Decimal
    data_age_sec: float | None
    inflight_orders: int
    unknown_orders: int
    orders_last_hour: int
    orders_today: int
    exchange_krw: Decimal | None = None  # None = unknown (not checked)
    exchange_eth: Decimal | None = None
    min_total: Decimal = Decimal(5000)
    fee_rate: Decimal = Decimal("0.0005")
    extra: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "price": str(self.price),
            "data_age_sec": self.data_age_sec,
            "inflight_orders": self.inflight_orders,
            "unknown_orders": self.unknown_orders,
            "orders_last_hour": self.orders_last_hour,
            "orders_today": self.orders_today,
            "exchange_krw": str(self.exchange_krw) if self.exchange_krw is not None else None,
            "exchange_eth": str(self.exchange_eth) if self.exchange_eth is not None else None,
        }


class GridRisk:
    # reasons that mean "something is wrong with the system" -> the service freezes the grid
    FREEZE_REASONS = frozenset({"ORDERS_PER_HOUR_GUARD", "ORDERS_PER_DAY_GUARD"})

    def __init__(self, cfg: EthGridConfig) -> None:
        self.cfg = cfg
        self.approved = 0
        self.rejected: dict[str, int] = {}

    def _reject(self, reason: str, ctx: GridRiskContext, **detail: Any) -> RiskDecision:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        return RiskDecision(False, reason, snapshot={**ctx.snapshot(), **detail})

    def evaluate(self, intent: Intent, eng: LiveGridEngine, ctx: GridRiskContext) -> RiskDecision:
        s = eng.s
        if s.frozen:
            return self._reject("FROZEN_NO_AUTO_EXECUTION", ctx, frozen_reason=s.frozen_reason)
        if ctx.unknown_orders > 0:
            return self._reject("UNKNOWN_ORDER_PENDING", ctx)
        if ctx.inflight_orders > 0 or s.pending is not None:
            return self._reject("INFLIGHT_ORDER", ctx)
        if ctx.data_age_sec is None or ctx.data_age_sec >= self.cfg.market_data_stale_sec:
            return self._reject("STALE_MARKET_DATA", ctx)
        if ctx.orders_last_hour >= self.cfg.max_orders_per_hour:
            return self._reject("ORDERS_PER_HOUR_GUARD", ctx, limit=self.cfg.max_orders_per_hour)
        if ctx.orders_today >= self.cfg.max_orders_per_day:
            return self._reject("ORDERS_PER_DAY_GUARD", ctx, limit=self.cfg.max_orders_per_day)

        if intent.side is Side.BID:
            krw = intent.krw
            if krw is None or krw <= 0:
                return self._reject("INVALID_AMOUNT", ctx)
            expected = eng.p.seed_krw if intent.kind == "SEED" else eng.current_unit
            if krw != expected:
                return self._reject("AMOUNT_NOT_POLICY", ctx, amount=str(krw), policy=str(expected))
            hard_cap = eng.policy.max_single_order_krw if eng.policy is not None else None
            if intent.kind != "SEED" and hard_cap is not None and krw > hard_cap:
                return self._reject("MAX_SINGLE_ORDER", ctx, amount=str(krw), cap=str(hard_cap))
            if krw < ctx.min_total:
                return self._reject("MIN_ORDER", ctx, amount=str(krw), min_total=str(ctx.min_total))
            need = (krw * (ONE + ctx.fee_rate)).quantize(Decimal("0.01"))
            if intent.kind == "SEED":
                if s.anchor != 0:
                    return self._reject("ALREADY_SEEDED", ctx)
                if s.cash - need < 0:
                    return self._reject("INSUFFICIENT_SLEEVE_CASH", ctx, cash=str(s.cash))
            else:
                if intent.level is None:
                    return self._reject("INVALID_LEVEL", ctx)
                ok, why = eng.buys_allowed(eng.level_price(intent.level))
                if not ok:
                    return self._reject(why, ctx, level=intent.level)
                if s.cash - need < eng.effective_limits(ctx.price)[1]:
                    return self._reject("MIN_CASH_RESERVE", ctx, cash=str(s.cash))
            if ctx.exchange_krw is not None and ctx.exchange_krw < need:
                return self._reject(
                    "EXCHANGE_KRW_INSUFFICIENT", ctx, need=str(need), have=str(ctx.exchange_krw)
                )
            self.approved += 1
            return RiskDecision(True, order_krw=krw, snapshot=ctx.snapshot())

        # SELL: never more than the lot, never below Upbit's minimum total
        lot = next((lot for lot in s.lots if lot.lot_id == intent.lot_id), None)
        if lot is None or intent.quantity is None or intent.quantity <= 0:
            return self._reject("LOT_NOT_FOUND", ctx, lot_id=intent.lot_id)
        if intent.quantity > lot.quantity:
            return self._reject("SELL_EXCEEDS_LOT", ctx, lot_id=lot.lot_id)
        if lot.lot_id in s.lots_bought_now:
            return self._reject("SAME_OBSERVATION_SELL", ctx, lot_id=lot.lot_id)
        if not meets_min_total(ctx.price, intent.quantity, ctx.min_total):
            return self._reject(
                "MIN_ORDER_SELL", ctx, value=str((ctx.price * intent.quantity).quantize(ONE))
            )
        if (
            ctx.exchange_eth is not None
            and ctx.exchange_eth + Decimal("0.00000001") < intent.quantity
        ):
            return self._reject(
                "EXCHANGE_ETH_INSUFFICIENT",
                ctx,
                need=str(intent.quantity),
                have=str(ctx.exchange_eth),
            )
        self.approved += 1
        return RiskDecision(True, order_volume=intent.quantity, snapshot=ctx.snapshot())
