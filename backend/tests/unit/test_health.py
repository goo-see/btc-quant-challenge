from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.exchange.upbit.ws_client import WsHealth
from app.monitoring.health import OutageClassifier, OutageKind, ProbeResult, RecoveryChecker
from app.portfolio.reconciler import ReconcileResult, ReconcileStatus


def _probe(result: ProbeResult | Exception):  # type: ignore[no-untyped-def]
    async def p() -> ProbeResult:
        if isinstance(result, Exception):
            raise result
        return result

    return p


async def test_fresh_data_is_no_outage() -> None:
    c = OutageClassifier(_probe(ProbeResult(True, 1.0)), stale_sec=120)
    assert await c.classify(WsHealth(), 5.0) is OutageKind.NONE


async def test_rest_down_means_exchange_outage() -> None:
    c = OutageClassifier(_probe(ProbeResult(False, None, "503")), stale_sec=120)
    assert await c.classify(WsHealth(), 300.0) is OutageKind.EXCHANGE_OUTAGE
    c = OutageClassifier(_probe(RuntimeError("connect")), stale_sec=120)
    assert await c.classify(WsHealth(), 300.0) is OutageKind.EXCHANGE_OUTAGE


async def test_exchange_reachable_but_not_trading_is_exchange_outage() -> None:
    c = OutageClassifier(_probe(ProbeResult(True, 900.0)), stale_sec=120)
    assert await c.classify(WsHealth(), 300.0) is OutageKind.EXCHANGE_OUTAGE


async def test_exchange_trading_but_our_feed_dead_is_local_fault() -> None:
    c = OutageClassifier(_probe(ProbeResult(True, 2.0)), stale_sec=120)
    assert await c.classify(WsHealth(connected=True), 300.0) is OutageKind.LOCAL_FAULT
    assert await c.classify(WsHealth(), None) is OutageKind.LOCAL_FAULT  # never received anything


async def test_unexplained_when_probe_gives_no_trade_age() -> None:
    c = OutageClassifier(_probe(ProbeResult(True, None)), stale_sec=120)
    assert await c.classify(WsHealth(), 300.0) is OutageKind.UNEXPLAINED


async def test_official_status_source_wins() -> None:
    class S:
        async def maintenance_announced(self) -> bool:
            return True

    c = OutageClassifier(_probe(ProbeResult(True, 2.0)), stale_sec=120, status_source=S())
    assert await c.classify(WsHealth(), 300.0) is OutageKind.EXCHANGE_OUTAGE


def _age_fresh(_now: datetime | None = None) -> float:
    return 3.0


def _age_stale(_now: datetime | None = None) -> float:
    return 500.0


def _rc(ok: bool) -> ReconcileResult:
    return ReconcileResult(
        ReconcileStatus.OK if ok else ReconcileStatus.FAIL, Decimal(0), Decimal(0), ok, ok, ok
    )


async def test_recovery_requires_all_six_checks() -> None:
    ws = WsHealth(connected=True)

    async def ping() -> bool:
        return True

    async def rec_ok() -> ReconcileResult:
        return _rc(True)

    r = RecoveryChecker(
        ws_health=lambda: ws,
        data_age=_age_fresh,
        rest_ping=ping,
        reconcile=rec_ok,
        stale_sec=120,
    )
    rep = await r.run()
    assert rep.all_passed and [c.name for c in rep.checks] == [
        "websocket_connected",
        "market_data_fresh",
        "rest_ok",
        "reconcile_balance",
        "reconcile_open_orders",
        "reconcile_position",
    ]
    r = RecoveryChecker(
        ws_health=lambda: ws,
        data_age=_age_stale,
        rest_ping=ping,
        reconcile=rec_ok,
        stale_sec=120,
    )
    assert (await r.run()).failed == ["market_data_fresh"]

    async def rec_bad() -> ReconcileResult:
        return _rc(False)

    r = RecoveryChecker(
        ws_health=lambda: ws,
        data_age=_age_fresh,
        rest_ping=ping,
        reconcile=rec_bad,
        stale_sec=120,
    )
    rep = await r.run()
    assert rep.reconciliation_failed and not rep.all_passed

    async def ping_down() -> bool:
        return False

    r = RecoveryChecker(
        ws_health=lambda: ws,
        data_age=_age_fresh,
        rest_ping=ping_down,
        reconcile=rec_ok,
        stale_sec=120,
    )
    rep = await r.run()
    assert "rest_ok" in rep.failed and not rep.reconciliation_failed  # skipped, not failed
