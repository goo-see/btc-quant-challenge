"""Risk Manager — the only component allowed to approve an order.

Rules (all from config, evaluated in order; first failure wins):
  execution policy (FROZEN -> nothing; EXIT_ONLY -> no BUY), UNKNOWN_ORDER, INFLIGHT_ORDER,
  DAILY_LOSS_LIMIT / MAX_DRAWDOWN (+kill -> HALTED/EXIT_ONLY; BUY rejected, protective SELL ok),
  SELL: position must exist, volume = held quantity (never more), min total;
  BUY: MAX_CONSECUTIVE_LOSSES, COOLDOWN, ORDERS_PER_HOUR, POSITION_STATE, MAX_CONCURRENT_POSITIONS,
  sizing (capped fraction of equity — never increased after losses), MIN_BALANCE.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config.settings import ModeRiskConfig, RiskConfig
from app.core.state import BotState, ExecutionMode, execution_mode
from app.core.types import Action, RiskDecision, Signal
from app.exchange.upbit.tick_size import meets_min_total
from app.risk.position_sizer import SizingParams, size_buy_krw

KillCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass(slots=True)
class RiskContext:
    now: datetime
    bot_state: BotState
    price: Decimal
    equity: Decimal
    krw: Decimal
    btc: Decimal
    has_position: bool
    open_positions: int
    daily_pnl: Decimal
    day_start_equity: Decimal
    drawdown: Decimal  # fraction, e.g. 0.05
    consecutive_losses: int
    last_loss_at: datetime | None
    orders_last_hour: int
    inflight_orders: int
    unknown_orders: int
    bot_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def execution_mode(self) -> ExecutionMode:
        return execution_mode(self.bot_state, self.bot_reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "bot_state": self.bot_state.value,
            "execution_mode": self.execution_mode.value,
            "price": str(self.price),
            "equity": str(self.equity),
            "krw": str(self.krw),
            "btc": str(self.btc),
            "daily_pnl": str(self.daily_pnl),
            "drawdown": str(self.drawdown),
            "consecutive_losses": self.consecutive_losses,
            "orders_last_hour": self.orders_last_hour,
            "inflight_orders": self.inflight_orders,
            "unknown_orders": self.unknown_orders,
        }


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig,
        mode_cfg: ModeRiskConfig,
        *,
        on_kill: KillCallback | None = None,
        min_order_krw: Decimal | None = None,
        fee_rate: Decimal | None = None,
    ) -> None:
        self.cfg = cfg
        self.mode_cfg = mode_cfg
        self._on_kill = on_kill
        # exchange-reported values (orders/chance) override config at startup
        self.min_order_krw = min_order_krw or cfg.min_order_krw
        self.fee_rate = fee_rate or cfg.fee_rate
        self.approved = 0
        self.rejected: dict[str, int] = {}

    def sizing_params(self) -> SizingParams:
        return SizingParams(
            max_position_pct=self.mode_cfg.max_position_pct,
            max_order_krw=self.mode_cfg.max_order_krw,
            min_order_krw=self.min_order_krw,
            fee_rate=self.fee_rate,
            minimum_krw_balance=self.cfg.minimum_krw_balance,
        )

    def _reject(self, reason: str, ctx: RiskContext, **detail: Any) -> RiskDecision:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        return RiskDecision(False, reason, snapshot={**ctx.snapshot(), **detail})

    async def evaluate(self, signal: Signal, ctx: RiskContext) -> RiskDecision:
        if signal.action is Action.HOLD:
            return self._reject("HOLD", ctx)

        # --- execution policy (layered on the state machine) --------------------
        mode = ctx.execution_mode
        if mode is ExecutionMode.FROZEN:
            return self._reject(
                "FROZEN_NO_AUTO_EXECUTION"
                if ctx.bot_state is BotState.HALTED
                else f"BOT_STATE_{ctx.bot_state.value}",
                ctx,
                state=ctx.bot_state.value,
                state_reason=ctx.bot_reason,
            )
        if signal.action is Action.BUY and mode is ExecutionMode.EXIT_ONLY:
            return self._reject(
                "EXIT_ONLY_NO_NEW_ENTRY",
                ctx,
                state=ctx.bot_state.value,
                state_reason=ctx.bot_reason,
            )
        if ctx.unknown_orders > 0:
            return self._reject("UNKNOWN_ORDER_PENDING", ctx)
        if ctx.inflight_orders > 0:
            return self._reject("INFLIGHT_ORDER", ctx)

        # --- loss limits: kill switch (-> HALTED/EXIT_ONLY); BUY rejected, SELL continues
        daily_limit = ctx.day_start_equity * self.cfg.daily_loss_limit_pct
        loss_reason: str | None = None
        if ctx.daily_pnl <= -daily_limit and daily_limit > 0:
            detail = {"daily_pnl": str(ctx.daily_pnl), "limit": str(-daily_limit)}
            if self._on_kill:
                await self._on_kill("DAILY_LOSS_LIMIT", detail)
            loss_reason = "DAILY_LOSS_LIMIT"
        elif ctx.drawdown >= self.cfg.max_drawdown_limit_pct:
            detail = {"drawdown": str(ctx.drawdown), "limit": str(self.cfg.max_drawdown_limit_pct)}
            if self._on_kill:
                await self._on_kill("MAX_DRAWDOWN", detail)
            loss_reason = "MAX_DRAWDOWN"
        if loss_reason and signal.action is Action.BUY:
            return self._reject(loss_reason, ctx, **detail)

        # --- SELL (protective exit): never increases exposure, never exceeds holdings ------
        if signal.action is Action.SELL:
            if not ctx.has_position or ctx.btc <= 0:
                return self._reject("POSITION_STATE_NO_POSITION", ctx)
            if not meets_min_total(ctx.price, ctx.btc, self.min_order_krw):
                return self._reject("MIN_ORDER_SELL", ctx, value=str(ctx.price * ctx.btc))
            self.approved += 1
            return RiskDecision(True, order_volume=ctx.btc, snapshot=ctx.snapshot())

        # --- BUY-only rules ---------------------------------------------------
        if ctx.consecutive_losses >= self.cfg.max_consecutive_losses:
            return self._reject("MAX_CONSECUTIVE_LOSSES", ctx)
        if ctx.last_loss_at is not None:
            until = ctx.last_loss_at + timedelta(minutes=self.cfg.cooldown_after_loss_min)
            if ctx.now < until:
                return self._reject("COOLDOWN", ctx, until=until.isoformat())
        if ctx.orders_last_hour >= self.cfg.max_orders_per_hour:
            return self._reject("ORDERS_PER_HOUR", ctx)
        if ctx.has_position:
            return self._reject("POSITION_STATE_ALREADY_LONG", ctx)
        if ctx.open_positions >= self.mode_cfg.max_concurrent_positions:
            return self._reject("MAX_CONCURRENT_POSITIONS", ctx)
        amount, why = size_buy_krw(ctx.equity, ctx.krw, self.sizing_params())
        if amount is None:
            return self._reject("MIN_ORDER", ctx, detail=why)
        if ctx.krw - amount * (1 + self.fee_rate) < self.cfg.minimum_krw_balance:
            return self._reject("MIN_BALANCE", ctx)
        self.approved += 1
        return RiskDecision(True, order_krw=amount, snapshot=ctx.snapshot())
