from __future__ import annotations

import pytest

from app.core.state import BotState, InvalidTransitionError, ManualResumeRequiredError, StateMachine


async def test_startup_is_maintenance_and_recovers_to_running() -> None:
    sm = StateMachine()
    assert sm.state is BotState.MAINTENANCE and not sm.trading_allowed
    await sm.transition(BotState.RUNNING, "RECOVERY_CHECKS_PASSED")
    assert sm.trading_allowed


async def test_halted_requires_manual_resume_and_goes_via_maintenance() -> None:
    sm = StateMachine()
    await sm.transition(BotState.RUNNING, "ok")
    await sm.transition(BotState.HALTED, "DAILY_LOSS_LIMIT")
    with pytest.raises(ManualResumeRequiredError):
        await sm.transition(BotState.MAINTENANCE, "auto")
    with pytest.raises(InvalidTransitionError):
        await sm.transition(
            BotState.RUNNING, "manual", manual=True
        )  # HALTED -> RUNNING never allowed
    await sm.transition(BotState.MAINTENANCE, "MANUAL_RESUME", manual=True)
    await sm.transition(BotState.RUNNING, "checks passed")
    assert [t.to_state for t in sm.history] == [
        BotState.RUNNING,
        BotState.HALTED,
        BotState.MAINTENANCE,
        BotState.RUNNING,
    ]


async def test_listeners_and_same_state_rejected() -> None:
    sm = StateMachine()
    seen = []

    async def listener(t):  # type: ignore[no-untyped-def]
        seen.append((t.from_state, t.to_state, t.reason))

    sm.subscribe(listener)
    await sm.transition(BotState.PAUSED, "pause")
    assert seen == [(BotState.MAINTENANCE, BotState.PAUSED, "pause")]
    with pytest.raises(InvalidTransitionError):
        await sm.transition(BotState.PAUSED, "again")


def test_execution_mode_mapping() -> None:
    from app.core.state import ExecutionMode, execution_mode

    assert execution_mode(BotState.RUNNING, "x") is ExecutionMode.ENTRY_AND_EXIT
    assert execution_mode(BotState.PAUSED, "MANUAL_PAUSE") is ExecutionMode.EXIT_ONLY
    assert execution_mode(BotState.HALTED, "DAILY_LOSS_LIMIT") is ExecutionMode.EXIT_ONLY
    assert execution_mode(BotState.HALTED, "MAX_DRAWDOWN") is ExecutionMode.EXIT_ONLY
    for r in (
        "RECONCILE_FAILED",
        "ORDER_UNKNOWN_UNRESOLVED",
        "DATABASE_FAILURE",
        "UNEXPLAINED_STALE",
        "API_ERRORS",
        "MANUAL_KILL",
    ):
        assert execution_mode(BotState.HALTED, r) is ExecutionMode.FROZEN
    assert execution_mode(BotState.MAINTENANCE, "EXCHANGE_OUTAGE") is ExecutionMode.FROZEN
