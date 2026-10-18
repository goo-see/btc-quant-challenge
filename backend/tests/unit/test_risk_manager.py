from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.settings import load_yaml_config
from app.core.state import BotState
from app.core.types import Action, Signal, Timeframe, TradingMode
from app.risk.risk_manager import RiskContext, RiskManager

CFG = load_yaml_config(Path(__file__).resolve().parents[3] / "config" / "config.yaml")
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _sig(action: Action) -> Signal:
    return Signal(
        action, 0.8, "test", NOW, "trend_momentum", Timeframe.M15, stop_price=Decimal(90_000_000)
    )


def _ctx(**over: Any) -> RiskContext:
    base: dict[str, Any] = {
        "now": NOW,
        "bot_state": BotState.RUNNING,
        "price": Decimal(100_000_000),
        "equity": Decimal(100_000),
        "krw": Decimal(100_000),
        "btc": Decimal(0),
        "has_position": False,
        "open_positions": 0,
        "daily_pnl": Decimal(0),
        "day_start_equity": Decimal(100_000),
        "drawdown": Decimal(0),
        "consecutive_losses": 0,
        "last_loss_at": None,
        "orders_last_hour": 0,
        "inflight_orders": 0,
        "unknown_orders": 0,
    }
    base.update(over)
    return RiskContext(**base)


class KillSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, reason: str, detail: dict[str, Any]) -> bool:
        self.calls.append(reason)
        return True


def _rm(spy: KillSpy | None = None) -> RiskManager:
    return RiskManager(CFG.risk, CFG.risk.for_mode(TradingMode.PAPER), on_kill=spy)


async def test_buy_approved_with_live_v1_sizing() -> None:
    d = await _rm().evaluate(_sig(Action.BUY), _ctx())
    assert d.approved and d.order_krw == Decimal(30000)
    d = await _rm().evaluate(_sig(Action.BUY), _ctx(equity=Decimal(50_000), krw=Decimal(50_000)))
    assert d.approved and d.order_krw == Decimal(15000)  # 30% of equity


async def test_hold_and_bot_state_gates() -> None:
    rm = _rm()
    assert (await rm.evaluate(_sig(Action.HOLD), _ctx())).reason == "HOLD"
    halted = _ctx(bot_state=BotState.HALTED, bot_reason="MANUAL_KILL")
    assert (await rm.evaluate(_sig(Action.BUY), halted)).reason == "FROZEN_NO_AUTO_EXECUTION"
    maint = _ctx(bot_state=BotState.MAINTENANCE)
    assert (await rm.evaluate(_sig(Action.BUY), maint)).reason == "BOT_STATE_MAINTENANCE"
    paused = _ctx(bot_state=BotState.PAUSED)
    assert (await rm.evaluate(_sig(Action.BUY), paused)).reason == "EXIT_ONLY_NO_NEW_ENTRY"
    # PAUSED = EXIT_ONLY: a protective SELL of the held quantity is still approved
    d = await rm.evaluate(
        _sig(Action.SELL), _ctx(bot_state=BotState.PAUSED, has_position=True, btc=Decimal("0.0003"))
    )
    assert d.approved and d.order_volume == Decimal("0.0003")


async def test_unknown_and_inflight_block_everything() -> None:
    rm = _rm()
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(unknown_orders=1))
    ).reason == "UNKNOWN_ORDER_PENDING"
    assert (
        await rm.evaluate(_sig(Action.SELL), _ctx(has_position=True, inflight_orders=1))
    ).reason == "INFLIGHT_ORDER"


async def test_daily_loss_limit_rejects_and_kills() -> None:
    spy = KillSpy()
    d = await _rm(spy).evaluate(_sig(Action.BUY), _ctx(daily_pnl=Decimal(-3000)))  # 3% of 100k
    assert not d.approved and d.reason == "DAILY_LOSS_LIMIT" and spy.calls == ["DAILY_LOSS_LIMIT"]
    d = await _rm(spy).evaluate(_sig(Action.BUY), _ctx(daily_pnl=Decimal(-2999)))
    assert d.approved


async def test_max_drawdown_kills_but_protective_sell_still_allowed() -> None:
    spy = KillSpy()
    d = await _rm(spy).evaluate(
        _sig(Action.SELL), _ctx(has_position=True, btc=Decimal("0.0003"), drawdown=Decimal("0.15"))
    )
    assert d.approved and d.order_volume == Decimal("0.0003") and spy.calls == ["MAX_DRAWDOWN"]
    assert (
        await _rm(spy).evaluate(_sig(Action.BUY), _ctx(drawdown=Decimal("0.15")))
    ).reason == "MAX_DRAWDOWN"
    # HALTED by a loss limit -> EXIT_ONLY: BUY blocked, SELL of the held quantity allowed (never more)
    loss = _ctx(bot_state=BotState.HALTED, bot_reason="DAILY_LOSS_LIMIT")
    assert (await _rm().evaluate(_sig(Action.BUY), loss)).reason == "EXIT_ONLY_NO_NEW_ENTRY"
    loss_pos = _ctx(
        bot_state=BotState.HALTED,
        bot_reason="DAILY_LOSS_LIMIT",
        has_position=True,
        btc=Decimal("0.0003"),
    )
    d = await _rm().evaluate(_sig(Action.SELL), loss_pos)
    assert d.approved and d.order_volume == Decimal("0.0003")
    # integrity halts are FROZEN: nothing executes automatically
    frozen = _ctx(
        bot_state=BotState.HALTED,
        bot_reason="RECONCILE_FAILED",
        has_position=True,
        btc=Decimal("0.0003"),
    )
    assert (await _rm().evaluate(_sig(Action.SELL), frozen)).reason == "FROZEN_NO_AUTO_EXECUTION"


async def test_sell_rules() -> None:
    rm = _rm()
    d = await rm.evaluate(_sig(Action.SELL), _ctx(has_position=True, btc=Decimal("0.0003")))
    assert d.approved and d.order_volume == Decimal("0.0003")
    assert (await rm.evaluate(_sig(Action.SELL), _ctx())).reason == "POSITION_STATE_NO_POSITION"
    d = await rm.evaluate(
        _sig(Action.SELL), _ctx(has_position=True, btc=Decimal("0.00004"))
    )  # 4,000 KRW < min
    assert d.reason == "MIN_ORDER_SELL"
    # SELL is NOT blocked by consecutive losses / cooldown / orders-per-hour
    d = await rm.evaluate(
        _sig(Action.SELL),
        _ctx(
            has_position=True,
            btc=Decimal("0.0003"),
            consecutive_losses=9,
            orders_last_hour=99,
            last_loss_at=NOW,
        ),
    )
    assert d.approved


async def test_buy_only_rules() -> None:
    rm = _rm()
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(consecutive_losses=4))
    ).reason == "MAX_CONSECUTIVE_LOSSES"
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(last_loss_at=NOW - timedelta(minutes=30)))
    ).reason == "COOLDOWN"
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(last_loss_at=NOW - timedelta(minutes=61)))
    ).approved
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(orders_last_hour=4))
    ).reason == "ORDERS_PER_HOUR"
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(has_position=True, btc=Decimal("0.0003")))
    ).reason == "POSITION_STATE_ALREADY_LONG"
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(open_positions=1))
    ).reason == "MAX_CONCURRENT_POSITIONS"
    assert (
        await rm.evaluate(_sig(Action.BUY), _ctx(equity=Decimal(10_000), krw=Decimal(10_000)))
    ).reason == "MIN_ORDER"


async def test_order_size_never_increases_after_losses() -> None:
    rm = _rm()
    a = await rm.evaluate(_sig(Action.BUY), _ctx(equity=Decimal(80_000), krw=Decimal(80_000)))
    b = await rm.evaluate(
        _sig(Action.BUY),
        _ctx(
            equity=Decimal(70_000),
            krw=Decimal(70_000),
            consecutive_losses=2,
            last_loss_at=NOW - timedelta(hours=2),
        ),
    )
    assert a.order_krw is not None and b.order_krw is not None and b.order_krw < a.order_krw
    assert rm.rejected == {}
