"""API dependencies: settings/repository singletons, control-endpoint authentication, audit."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.config.settings import Settings
from app.database.repository import Repository

CONTROL_TOKEN_ENV = "DASHBOARD_CONTROL_TOKEN"  # noqa: S105 - env var NAME, not a secret
CSRF_HEADER_VALUE = "quant-dashboard"


@dataclass(slots=True)
class ApiState:
    settings: Settings
    repo: Repository
    eth_repo: Repository | None = None  # ETH grid LIVE DB (opened lazily, read-only usage)
    eth_engine: object | None = None


def get_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.api
    return state


def get_repo(state: Annotated[ApiState, Depends(get_state)]) -> Repository:
    return state.repo


def get_settings(state: Annotated[ApiState, Depends(get_state)]) -> Settings:
    return state.settings


def get_eth_repo(state: Annotated[ApiState, Depends(get_state)]) -> Repository | None:
    """Repository on the ETH grid DB if that file exists (the grid process creates it)."""
    if state.eth_repo is not None:
        return state.eth_repo
    url = state.settings.config.eth_grid.database_url
    path = url.split("///", 1)[-1]
    if not path or path == ":memory:" or not Path(path).exists():
        return None
    from app.database.engine import create_engine

    engine = create_engine(url)
    state.eth_engine = engine
    state.eth_repo = Repository(engine)
    return state.eth_repo


@dataclass(frozen=True, slots=True)
class Operator:
    login: str
    ip: str
    user_agent: str

    def audit(self) -> dict[str, str]:
        return {"login": self.login, "ip": self.ip, "user_agent": self.user_agent[:200]}


def _configured_token(request: Request) -> str:
    state: ApiState | None = getattr(request.app.state, "api", None)
    if state is not None and state.settings.env.dashboard_control_token is not None:
        return state.settings.env.dashboard_control_token.get_secret_value()
    return os.environ.get(CONTROL_TOKEN_ENV, "")


def require_control_auth(
    request: Request,
    x_control_token: Annotated[str | None, Header()] = None,
    x_requested_with: Annotated[str | None, Header()] = None,
    tailscale_user_login: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> Operator:
    """Mutating endpoints require a shared secret + a custom header (CSRF) and are audited.

    The API never sets cookies, so a browser cannot be tricked into an authenticated request;
    the custom X-Requested-With header additionally blocks simple cross-site form posts.
    Tailscale Serve injects Tailscale-User-Login (and strips client-supplied copies), which we
    record for the audit trail — being on the tailnet alone is NOT sufficient.
    """
    expected = _configured_token(request)
    if not expected or len(expected) < 16:
        raise HTTPException(
            503, "control endpoints disabled: DASHBOARD_CONTROL_TOKEN not configured"
        )
    if not x_control_token or not hmac.compare_digest(x_control_token, expected):
        raise HTTPException(401, "invalid control token")
    if x_requested_with != CSRF_HEADER_VALUE:
        raise HTTPException(403, "missing X-Requested-With header")
    client_ip = request.client.host if request.client else "?"
    return Operator(
        login=tailscale_user_login or "local", ip=client_ip, user_agent=user_agent or ""
    )
