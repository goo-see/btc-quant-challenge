"""Emergency kill switch -> BotState.HALTED. Only a manual resume can leave HALTED."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.events import Event, EventBus, EventLevel, EventType
from app.core.logging import get_logger
from app.core.state import BotState, InvalidTransitionError, StateMachine
from app.database.repository import Repository

log = get_logger("kill_switch")


class KillSwitch:
    def __init__(self, sm: StateMachine, repo: Repository | None, bus: EventBus) -> None:
        self._sm = sm
        self._repo = repo
        self._bus = bus
        self.triggered_at: datetime | None = None
        self.reason: str | None = None
        self.trigger_count = 0

    @property
    def active(self) -> bool:
        return self._sm.state is BotState.HALTED

    async def trigger(self, reason: str, detail: dict[str, Any] | None = None) -> bool:
        """Halt trading. Returns True if this call caused the halt (idempotent otherwise)."""
        detail = detail or {}
        log.critical("kill_switch", reason=reason, **detail)
        if self.active:
            return False
        self.trigger_count += 1
        self.triggered_at = datetime.now(UTC)
        self.reason = reason
        try:
            await self._sm.transition(BotState.HALTED, reason)
        except InvalidTransitionError as exc:  # pragma: no cover - HALTED is reachable from all
            log.error("kill_switch_transition_failed", error=str(exc))
            return False
        await self._bus.publish(
            Event(EventType.KILL_SWITCH, f"KILL SWITCH: {reason}", EventLevel.CRITICAL, detail)
        )
        if self._repo is not None:
            await self._repo.set_state(
                "kill_switch",
                {"reason": reason, "detail": detail, "at": self.triggered_at.isoformat()},
            )
        return True
