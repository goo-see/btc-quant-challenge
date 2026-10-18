"""Control endpoints (mutating): PAUSE / RESUME / KILL -> persisted commands consumed by the trader.

Protected by require_control_auth (shared token + CSRF header), require an explicit confirmation
phrase, are idempotent on command_id, carry a TTL, and are audited (operator login/ip/ua).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import Operator, get_repo, get_settings, require_control_auth
from app.config.settings import Settings
from app.database.repository import Repository

router = APIRouter(prefix="/api/control")

CommandType = Literal["PAUSE", "RESUME", "KILL"]


class CommandRequest(BaseModel):
    command_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    confirm: str = Field(description="must equal the command type, e.g. 'KILL'")


@router.post("/{command}")
async def post_command(
    command: CommandType,
    body: CommandRequest,
    op: Annotated[Operator, Depends(require_control_auth)],
    repo: Annotated[Repository, Depends(get_repo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if body.confirm != command:
        raise HTTPException(400, f"confirmation mismatch: type '{command}' to confirm")
    ttl = (
        settings.config.system.kill_command_ttl_sec
        if command == "KILL"
        else settings.config.system.command_ttl_sec
    )
    row = await repo.add_command(
        command, op.login, confirmed=True, command_id=body.command_id, ttl_sec=ttl, audit=op.audit()
    )
    await repo.add_event(
        "WARN",
        "COMMAND_REQUESTED",
        f"{command} requested by {op.login}",
        {"command_id": row.command_id, **op.audit()},
    )
    return {
        "command_id": row.command_id,
        "type": row.type,
        "status": row.status,
        "requested_at": _iso(row.ts),
        "expires_at": _iso(row.expires_at) if row.expires_at else None,
    }


def _iso(dt: datetime) -> str:
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).isoformat()
