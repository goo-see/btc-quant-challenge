"""Health assessment, outage classification and recovery checks.

Outage classification (no hard-coded maintenance windows):
  data stale  ->  probe the exchange over REST
    REST unreachable / 5xx / ticker itself stale  -> EXCHANGE_OUTAGE  -> MAINTENANCE
    REST fine and fresh (only OUR feed is dead)   -> LOCAL_FAULT -> reconnect grace, then HALTED
    cannot decide (mixed signals)                 -> UNEXPLAINED      -> HALTED
An optional official StatusSource (e.g. Upbit announcement WebSocket) can be plugged in and
takes precedence when it reports maintenance.

Recovery (MAINTENANCE -> RUNNING) requires ALL of: websocket connected, fresh market data,
REST ok, balance reconciliation, open-order reconciliation, position reconciliation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.core.logging import get_logger
from app.exchange.upbit.ws_client import WsHealth
from app.portfolio.reconciler import ReconcileResult

log = get_logger("health")


class OutageKind(StrEnum):
    NONE = "NONE"
    EXCHANGE_OUTAGE = "EXCHANGE_OUTAGE"
    LOCAL_FAULT = "LOCAL_FAULT"
    UNEXPLAINED = "UNEXPLAINED"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    rest_ok: bool
    exchange_trade_age_sec: float | None  # age of the latest trade the exchange reports
    error: str | None = None


ExchangeProbe = Callable[[], Awaitable[ProbeResult]]


class StatusSource(Protocol):
    """Official exchange status (announcements). Returns True when maintenance is announced."""

    async def maintenance_announced(self) -> bool: ...


@dataclass(slots=True)
class HealthSnapshot:
    ts: datetime
    ws_connected: bool
    data_age_sec: float | None
    rest_ok: bool
    rest_latency_ms: float
    rest_consecutive_errors: int
    db_ok: bool
    heartbeat_age_sec: float | None
    reconnect_count: int
    message_count: int

    def to_row(
        self, uptime_sec: int, last_data_ts: datetime | None, heartbeat_ts: datetime
    ) -> dict[str, object]:
        return {
            "ws_connected": self.ws_connected,
            "reconnect_count": self.reconnect_count,
            "msg_count": self.message_count,
            "last_data_ts": last_data_ts,
            "rest_latency_ms": self.rest_latency_ms,
            "rest_errors": self.rest_consecutive_errors,
            "db_ok": self.db_ok,
            "heartbeat_ts": heartbeat_ts,
            "uptime_sec": uptime_sec,
        }


class OutageClassifier:
    def __init__(
        self, probe: ExchangeProbe, *, stale_sec: float, status_source: StatusSource | None = None
    ) -> None:
        self._probe = probe
        self._stale = stale_sec
        self._status = status_source
        self.last_probe: ProbeResult | None = None

    async def classify(self, ws: WsHealth, data_age_sec: float | None) -> OutageKind:
        if data_age_sec is not None and data_age_sec < self._stale:
            return OutageKind.NONE
        if self._status is not None:
            try:
                if await self._status.maintenance_announced():
                    return OutageKind.EXCHANGE_OUTAGE
            except Exception as exc:
                log.warning("status_source_failed", error=repr(exc))
        try:
            probe = await self._probe()
        except Exception as exc:
            probe = ProbeResult(rest_ok=False, exchange_trade_age_sec=None, error=repr(exc))
        self.last_probe = probe
        if not probe.rest_ok:
            return OutageKind.EXCHANGE_OUTAGE
        if probe.exchange_trade_age_sec is not None and probe.exchange_trade_age_sec >= self._stale:
            return (
                OutageKind.EXCHANGE_OUTAGE
            )  # exchange reachable but nothing is trading -> maintenance/halt
        if probe.exchange_trade_age_sec is not None:
            return OutageKind.LOCAL_FAULT  # exchange is trading; our feed is the problem
        return OutageKind.UNEXPLAINED


@dataclass(frozen=True, slots=True)
class RecoveryCheck:
    name: str
    passed: bool
    detail: str
    skipped: bool = False  # could not be evaluated (e.g. REST down) — not a verdict


@dataclass(slots=True)
class RecoveryReport:
    checks: list[RecoveryCheck] = field(default_factory=list)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    @property
    def reconciliation_failed(self) -> bool:
        return any(
            c.name.startswith("reconcile_") and not c.passed and not c.skipped for c in self.checks
        )


class RecoveryChecker:
    def __init__(
        self,
        *,
        ws_health: Callable[[], WsHealth],
        data_age: Callable[[datetime | None], float | None],
        rest_ping: Callable[[], Awaitable[bool]],
        reconcile: Callable[[], Awaitable[ReconcileResult]],
        stale_sec: float,
    ) -> None:
        self._ws = ws_health
        self._age = data_age
        self._ping = rest_ping
        self._reconcile = reconcile
        self._stale = stale_sec
        self.last: RecoveryReport | None = None

    async def run(self, now: datetime | None = None) -> RecoveryReport:
        rep = RecoveryReport(at=now or datetime.now(UTC))
        ws = self._ws()
        rep.checks.append(
            RecoveryCheck("websocket_connected", ws.connected, f"connected={ws.connected}")
        )
        age = self._age(now)
        fresh = age is not None and age < self._stale
        rep.checks.append(RecoveryCheck("market_data_fresh", fresh, f"age={age}"))
        try:
            rest_ok = await self._ping()
        except Exception as exc:
            rest_ok = False
            log.warning("recovery_rest_ping_failed", error=repr(exc))
        rep.checks.append(RecoveryCheck("rest_ok", rest_ok, f"rest_ok={rest_ok}"))
        if rest_ok:
            try:
                r = await self._reconcile()
                rep.checks.append(
                    RecoveryCheck(
                        "reconcile_balance",
                        r.balance_ok,
                        f"krw_diff={r.krw_diff} btc_diff={r.btc_diff}",
                    )
                )
                rep.checks.append(
                    RecoveryCheck(
                        "reconcile_open_orders",
                        r.open_orders_ok,
                        str(r.details.get("exchange_open")),
                    )
                )
                rep.checks.append(
                    RecoveryCheck(
                        "reconcile_position", r.position_ok, str(r.details.get("open_trade_id"))
                    )
                )
            except Exception as exc:
                for n in ("reconcile_balance", "reconcile_open_orders", "reconcile_position"):
                    rep.checks.append(RecoveryCheck(n, False, f"error={exc!r}"))
        else:
            for n in ("reconcile_balance", "reconcile_open_orders", "reconcile_position"):
                rep.checks.append(RecoveryCheck(n, False, "skipped: rest down", skipped=True))
        self.last = rep
        return rep
