"""Command lifecycle in the trader: TTL expiry, ACK/EXECUTED/FAILED, restart-safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.state import BotState
from tests.integration.test_trader_service import _service, _st, _tick


async def test_expired_command_is_never_executed_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, _, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    await svc.on_ws_message(_tick(Decimal(100_000_000), t0))
    await svc.supervisor_tick(t0)
    assert _st(svc) is BotState.RUNNING
    # a KILL posted 20 minutes "ago" with a 10-minute TTL (trader was down) -> EXPIRED, not executed
    row = await repo.add_command("KILL", "op", confirmed=True, command_id="old-kill", ttl_sec=600)
    await svc.process_commands(now=datetime.now(UTC) + timedelta(minutes=20))
    got = await repo.get_command("old-kill")
    assert got is not None and got.status == "EXPIRED" and got.failure_reason == "ttl expired"
    assert _st(svc) is BotState.RUNNING
    # a fresh KILL inside its TTL executes and is fully audited
    await repo.add_command(
        "KILL", "op", confirmed=True, command_id="new-kill", ttl_sec=600, audit={"ip": "100.64.0.1"}
    )
    await svc.process_commands()
    got = await repo.get_command("new-kill")
    assert (
        got is not None
        and got.status == "EXECUTED"
        and got.acknowledged_at is not None
        and got.executed_at is not None
    )
    assert _st(svc) is BotState.HALTED
    # idempotent re-post of the same command_id returns the executed row, no second execution
    again = await repo.add_command("KILL", "op", confirmed=True, command_id="new-kill", ttl_sec=600)
    assert again.status == "EXECUTED" and again.id == row.id + 1
    await svc.stop()


async def test_invalid_transition_marks_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, repo, _, _ = await _service(monkeypatch, tmp_path)
    await svc.start(run_loops=False)
    # PAUSE while still in MAINTENANCE is allowed; a second PAUSE is an invalid (same-state) transition
    await repo.add_command("PAUSE", "op", confirmed=True, command_id="p1", ttl_sec=600)
    await svc.process_commands()
    assert _st(svc) is BotState.PAUSED
    await repo.add_command("PAUSE", "op", confirmed=True, command_id="p2", ttl_sec=600)
    await svc.process_commands()
    got = await repo.get_command("p2")
    assert (
        got is not None and got.status == "FAILED" and "already PAUSED" in str(got.failure_reason)
    )
    await svc.stop()
