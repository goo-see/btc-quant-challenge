"""API/Dashboard process. Binds to loopback ONLY; reach it through `tailscale serve`."""

from __future__ import annotations

import ipaddress

import uvicorn

from app.api.app import create_app
from app.config.settings import Settings
from app.core.logging import configure_logging, get_logger

log = get_logger("api")


def assert_loopback(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise SystemExit(f"API_HOST must be a loopback IP, got {host!r}") from exc
    if not ip.is_loopback:
        raise SystemExit(
            f"Refusing to bind API to non-loopback address {host} (use tailscale serve)"
        )


def main() -> None:
    settings = Settings.load()
    configure_logging(settings.config.system.log_level, None, json_output=False)
    assert_loopback(settings.env.api_host)
    app = create_app(settings)
    log.info("api_starting", host=settings.env.api_host, port=settings.env.api_port)
    uvicorn.run(app, host=settings.env.api_host, port=settings.env.api_port, log_level="warning")


if __name__ == "__main__":
    main()
