from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.monitoring.network import append_observation, classify_ip, evaluate_static_ip


def test_classify_rejects_tailscale_private_loopback_ipv6() -> None:
    assert classify_ip("1.2.3.4") == ("1.2.3.4", "public")
    assert classify_ip("100.64.0.5")[1] == "tailscale_cgnat"
    assert classify_ip("10.0.0.5")[1] == "private_or_reserved"
    assert classify_ip("10.0.0.5")[1] == "private_or_reserved"
    assert classify_ip("127.0.0.1")[1] == "private_or_reserved"
    assert classify_ip("2001:db8::1")[1] == "not_ipv4"
    assert classify_ip("http_503")[1] == "not_an_ip"


def _hist(ips: Sequence[str | None], hours_apart: float = 1.0) -> list[dict[str, object]]:
    t0 = datetime.now(UTC) - timedelta(hours=hours_apart * (len(ips) - 1))
    return [
        {"at": (t0 + timedelta(hours=hours_apart * i)).isoformat(), "ip": ip, "detail": {}}
        for i, ip in enumerate(ips)
    ]


def test_gate_not_configured_or_no_observations() -> None:
    v = evaluate_static_ip(None, _hist(["1.2.3.4"] * 30), min_hours=24)
    assert not v.passed and "not_configured" in v.reason
    v = evaluate_static_ip("100.64.0.5", _hist(["100.64.0.5"] * 30), min_hours=24)
    assert not v.passed and "tailscale_cgnat" in v.reason  # never whitelist a tailscale IP
    v = evaluate_static_ip("1.2.3.4", [], min_hours=24)
    assert not v.passed and "no observations" in v.reason


def test_gate_requires_stability_and_span() -> None:
    ip = "1.2.3.4"
    v = evaluate_static_ip(ip, _hist([ip] * 10), min_hours=24)  # 9h span
    assert not v.passed and "9.0h < 24h" in v.reason and v.pending  # same IP, short span
    v = evaluate_static_ip(ip, _hist([ip] * 26), min_hours=24)  # 25h span
    assert v.passed and v.reason.startswith("IP_STABILITY_MINIMUM_PASS") and v.span_hours >= 24
    assert not v.pending
    v = evaluate_static_ip(ip, _hist([ip] * 20 + ["1.2.3.9"] + [ip] * 6), min_hours=24)
    assert not v.passed and "!= configured" in v.reason
    v = evaluate_static_ip(ip, _hist([ip] * 20 + [None] + [ip] * 6), min_hours=24)
    assert not v.passed and "inconsistent" in v.reason


def test_append_observation_caps_history() -> None:
    h: list[dict[str, object]] = []
    for _ in range(600):
        h = append_observation(h, "1.2.3.4", {})
    assert len(h) == 500 and h[-1]["ip"] == "1.2.3.4"
