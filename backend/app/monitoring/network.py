"""Outbound public IPv4 observation and the STATIC_PUBLIC_IP gate.

Upbit whitelists the real outbound public IPv4 of this machine. Tailscale (100.64/10), LAN
(RFC1918) and loopback addresses are never acceptable. The gate passes only when:
  - live_gate.static_public_ip is configured (the operator's declared static IP), and
  - every observation stored so far equals it, and
  - observations span at least live_gate.static_ip_min_observation_hours.
Nothing here ever prints secrets; IPs are not secrets but are still kept out of code/config VCS
history by living in config.yaml (operator-edited) and bot_state.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.logging import get_logger

log = get_logger("network")

PUBLIC_IP_SOURCES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def classify_ip(text: str) -> tuple[str | None, str]:
    """Return (ipv4 or None, reason)."""
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return None, "not_an_ip"
    if ip.version != 4:
        return None, "not_ipv4"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return None, "private_or_reserved"
    if ip in _CGNAT:
        return None, "tailscale_cgnat"
    return str(ip), "public"


async def observe_public_ipv4(request_timeout: float = 8.0) -> tuple[str | None, dict[str, str]]:
    """Query several sources; accept only when all successful answers agree on a public IPv4."""
    results: dict[str, str] = {}
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")  # force IPv4  # noqa: S104
    async with httpx.AsyncClient(timeout=request_timeout, transport=transport) as client:

        async def one(url: str) -> None:
            try:
                r = await client.get(url)
                results[url] = r.text.strip() if r.status_code == 200 else f"http_{r.status_code}"
            except httpx.HTTPError as exc:
                results[url] = f"error:{type(exc).__name__}"

        await asyncio.gather(*(one(u) for u in PUBLIC_IP_SOURCES))
    good = {classify_ip(v)[0] for v in results.values() if classify_ip(v)[0]}
    if len(good) == 1:
        return good.pop(), results
    return None, results


@dataclass(slots=True)
class IpObservation:
    at: str
    ip: str | None
    detail: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class StaticIpVerdict:
    passed: bool
    reason: str
    configured: str | None
    observed_ips: list[str]
    observations: int
    span_hours: float
    latest: str | None
    pending: bool = False  # configured IP matches every observation but span is still short

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "pending": self.pending,
            "reason": self.reason,
            "configured": self.configured,
            "observed_ips": self.observed_ips,
            "observations": self.observations,
            "span_hours": round(self.span_hours, 2),
            "latest": self.latest,
        }


def evaluate_static_ip(
    configured: str | None, history: list[dict[str, Any]], *, min_hours: float
) -> StaticIpVerdict:
    obs = [IpObservation(str(h.get("at")), h.get("ip"), dict(h.get("detail", {}))) for h in history]
    ips = sorted({o.ip for o in obs if o.ip})
    latest = obs[-1].ip if obs else None
    span = 0.0
    if len(obs) >= 2:
        span = (
            datetime.fromisoformat(obs[-1].at) - datetime.fromisoformat(obs[0].at)
        ).total_seconds() / 3600
    cfg_ip, cfg_reason = classify_ip(configured) if configured else (None, "not_configured")
    if cfg_ip is None:
        return StaticIpVerdict(
            False,
            f"STATIC_PUBLIC_IP_NOT_VERIFIED ({cfg_reason})",
            configured,
            ips,
            len(obs),
            span,
            latest,
        )
    if not obs:
        return StaticIpVerdict(
            False, "STATIC_PUBLIC_IP_NOT_VERIFIED (no observations)", cfg_ip, ips, 0, 0.0, None
        )
    if any(o.ip is None for o in obs):
        return StaticIpVerdict(
            False,
            "STATIC_PUBLIC_IP_NOT_VERIFIED (inconsistent sources)",
            cfg_ip,
            ips,
            len(obs),
            span,
            latest,
        )
    if ips != [cfg_ip]:
        return StaticIpVerdict(
            False,
            f"STATIC_PUBLIC_IP_NOT_VERIFIED (observed {ips} != configured)",
            cfg_ip,
            ips,
            len(obs),
            span,
            latest,
        )
    if span < min_hours:
        return StaticIpVerdict(
            False,
            f"STATIC_PUBLIC_IP_NOT_VERIFIED (observed {span:.1f}h < {min_hours:.0f}h)",
            cfg_ip,
            ips,
            len(obs),
            span,
            latest,
            pending=True,
        )
    return StaticIpVerdict(
        passed=True,
        reason="IP_STABILITY_MINIMUM_PASS (24h+ stable; not proof of ISP static IP)",
        configured=cfg_ip,
        observed_ips=ips,
        observations=len(obs),
        span_hours=span,
        latest=latest,
    )


def append_observation(
    history: list[dict[str, Any]], ip: str | None, detail: dict[str, str], *, keep: int = 500
) -> list[dict[str, Any]]:
    history = [*history, {"at": datetime.now(UTC).isoformat(), "ip": ip, "detail": detail}]
    return history[-keep:]
