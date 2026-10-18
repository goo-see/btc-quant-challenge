"""In-process async event bus. Every important state change is published here and persisted."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class EventType(StrEnum):
    BOT_STARTED = "BOT_STARTED"
    BOT_PAUSED = "BOT_PAUSED"
    BOT_RESUMED = "BOT_RESUMED"
    BOT_STOPPED = "BOT_STOPPED"
    WEBSOCKET_CONNECTED = "WEBSOCKET_CONNECTED"
    WEBSOCKET_DISCONNECTED = "WEBSOCKET_DISCONNECTED"
    WEBSOCKET_RECONNECTED = "WEBSOCKET_RECONNECTED"
    MARKET_DATA_GAP = "MARKET_DATA_GAP"
    CANDLE_CLOSED = "CANDLE_CLOSED"
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RECONCILE_OK = "RECONCILE_OK"
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
    RECONCILE_DEFERRED = "RECONCILE_DEFERRED"
    SLEEVE_TRANSFER = "SLEEVE_TRANSFER"
    API_ERROR = "API_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    DATABASE_ERROR = "DATABASE_ERROR"
    KILL_SWITCH = "KILL_SWITCH"
    HEARTBEAT = "HEARTBEAT"
    DAILY_REPORT = "DAILY_REPORT"


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    message: str
    level: EventLevel = EventLevel.INFO
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Fan-out bus. Handler exceptions are isolated so one bad subscriber cannot break trading."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._by_type: dict[EventType, list[Handler]] = {}
        self.handler_errors = 0

    def subscribe(self, handler: Handler, *types: EventType) -> None:
        if not types:
            self._handlers.append(handler)
            return
        for t in types:
            self._by_type.setdefault(t, []).append(handler)

    async def publish(self, event: Event) -> None:
        handlers = [*self._handlers, *self._by_type.get(event.type, [])]
        results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                self.handler_errors += 1
                # Logged by the subscriber layer; we deliberately do not re-raise.
                from app.core.logging import get_logger

                get_logger("eventbus").error(
                    "event_handler_failed", event=event.type.value, error=repr(r)
                )
