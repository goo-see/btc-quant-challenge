"""Bot state machine.

RUNNING      normal operation, new orders allowed
PAUSED       operator pause, no new orders, data keeps flowing
MAINTENANCE  expected/explained exchange data outage OR startup recovery; no new orders;
             auto-returns to RUNNING when ALL recovery checks pass
HALTED       kill switch; no new orders; ONLY an explicit manual resume can leave this state
             (resume -> MAINTENANCE -> recovery checks -> RUNNING)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class BotState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    MAINTENANCE = "MAINTENANCE"
    HALTED = "HALTED"


_ALLOWED: dict[BotState, frozenset[BotState]] = {
    BotState.MAINTENANCE: frozenset({BotState.RUNNING, BotState.PAUSED, BotState.HALTED}),
    BotState.RUNNING: frozenset({BotState.PAUSED, BotState.MAINTENANCE, BotState.HALTED}),
    BotState.PAUSED: frozenset({BotState.RUNNING, BotState.MAINTENANCE, BotState.HALTED}),
    BotState.HALTED: frozenset({BotState.MAINTENANCE}),
}


class InvalidTransitionError(RuntimeError):
    pass


class ManualResumeRequiredError(InvalidTransitionError):
    pass


@dataclass(frozen=True, slots=True)
class Transition:
    at: datetime
    from_state: BotState
    to_state: BotState
    reason: str
    manual: bool


Listener = Callable[[Transition], Awaitable[None]]


@dataclass(slots=True)
class StateMachine:
    state: BotState = BotState.MAINTENANCE
    reason: str = "STARTUP"
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    history: list[Transition] = field(default_factory=list)
    _listeners: list[Listener] = field(default_factory=list)

    @property
    def trading_allowed(self) -> bool:
        return self.state is BotState.RUNNING

    @property
    def execution_mode(self) -> ExecutionMode:
        return execution_mode(self.state, self.reason)

    @property
    def exits_allowed(self) -> bool:
        return self.execution_mode is not ExecutionMode.FROZEN

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can(self, to: BotState) -> bool:
        return to in _ALLOWED[self.state]

    async def transition(self, to: BotState, reason: str, *, manual: bool = False) -> Transition:
        if to == self.state:
            raise InvalidTransitionError(f"already {to}")
        if not self.can(to):
            raise InvalidTransitionError(f"{self.state} -> {to} not allowed")
        if self.state is BotState.HALTED and not manual:
            raise ManualResumeRequiredError("leaving HALTED requires an explicit manual resume")
        t = Transition(datetime.now(UTC), self.state, to, reason, manual)
        self.state, self.reason, self.since = to, reason, t.at
        self.history.append(t)
        for cb in self._listeners:
            await cb(t)
        return t

    def restore(self, state: BotState, reason: str, since: datetime) -> None:
        """Rehydrate from persisted state without firing listeners (process restart)."""
        self.state, self.reason, self.since = state, reason, since


class ExecutionMode(StrEnum):
    """Order policy derived from (state, reason). Layered on the 4-state machine, never replaces it.

    ENTRY_AND_EXIT  RUNNING
    EXIT_ONLY       PAUSED, or HALTED by a loss limit (DAILY_LOSS_LIMIT / MAX_DRAWDOWN):
                    no new BUY; protective exits (ATR/trailing stop, channel, trend-loss) allowed
    FROZEN          MAINTENANCE (exchange untrusted) or HALTED for integrity reasons
                    (reconcile, UNKNOWN order, DB, stale data, API errors, manual kill):
                    no automatic orders at all — explicit operator action required
    """

    ENTRY_AND_EXIT = "ENTRY_AND_EXIT"
    EXIT_ONLY = "EXIT_ONLY"
    FROZEN = "FROZEN"


LOSS_LIMIT_REASONS = frozenset({"DAILY_LOSS_LIMIT", "MAX_DRAWDOWN"})


def execution_mode(state: BotState, reason: str) -> ExecutionMode:
    if state is BotState.RUNNING:
        return ExecutionMode.ENTRY_AND_EXIT
    if state is BotState.PAUSED:
        return ExecutionMode.EXIT_ONLY
    if state is BotState.HALTED and reason in LOSS_LIMIT_REASONS:
        return ExecutionMode.EXIT_ONLY
    return ExecutionMode.FROZEN
