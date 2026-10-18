"""Tailscale status via the local CLI (`tailscale status --json`), cached.

Under launchd there is no GUI session and the macOS app CLI fails ("The Tailscale GUI failed to
start"); we then fall back to interface inspection: a utun interface carrying a 100.64/10 address
means the tailnet is up (DNS name unknown in that mode)."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

CLI_CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
)

_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _cli() -> str | None:
    for c in CLI_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("tailscale")


async def tailscale_status(ttl_sec: float = 30.0) -> dict[str, Any]:
    now = time.monotonic()
    if _cache["value"] is not None and now - float(_cache["at"]) < ttl_sec:
        return dict(_cache["value"])
    cli = _cli()
    if cli is None:
        value: dict[str, Any] = {"installed": False, "state": "NOT_INSTALLED"}
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                cli,
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                value = {"installed": True, "state": "ERROR", "detail": err.decode()[:200]}
            else:
                data = json.loads(out.decode())
                self_ = data.get("Self", {}) or {}
                value = {
                    "installed": True,
                    "state": str(data.get("BackendState", "?")),
                    "dns_name": str(self_.get("DNSName", "")).rstrip("."),
                    "tailscale_ips": list(self_.get("TailscaleIPs", []) or []),
                    "online": bool(self_.get("Online", False)),
                    "magic_dns_suffix": str(data.get("MagicDNSSuffix", "")),
                }
        except (TimeoutError, OSError, json.JSONDecodeError) as exc:
            value = await _interface_fallback(repr(exc)[:120])
        if value.get("state") == "ERROR":
            value = await _interface_fallback(str(value.get("detail", ""))[:120])
    _cache["at"], _cache["value"] = now, value
    return dict(value)


async def _interface_fallback(detail: str) -> dict[str, Any]:
    """No CLI session (launchd): infer tailnet state from a 100.64/10 utun address."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ifconfig", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (TimeoutError, OSError) as exc:
        return {"installed": True, "state": "ERROR", "detail": f"{detail}; ifconfig {exc!r}"[:200]}
    ips = _cgnat_ips(out.decode(errors="replace"))
    if ips:
        return {
            "installed": True,
            "state": "Running",
            "tailscale_ips": ips,
            "source": "interface",
            "detail": "cli unavailable under launchd",
        }
    return {"installed": True, "state": "Stopped", "source": "interface", "detail": detail}


def _cgnat_ips(ifconfig_text: str) -> list[str]:
    import ipaddress

    net = ipaddress.ip_network("100.64.0.0/10")
    found: list[str] = []
    for line in ifconfig_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            try:
                ip = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if ip.version == 4 and ip in net:
                found.append(str(ip))
    return found
