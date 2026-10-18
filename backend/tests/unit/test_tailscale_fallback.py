from __future__ import annotations

from app.api.tailscale import _cgnat_ips

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.5 netmask 0xffffff00 broadcast 10.0.0.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.64.0.5 --> 100.64.0.5 netmask 0xffffffff
"""


def test_cgnat_ip_detection() -> None:
    assert _cgnat_ips(IFCONFIG) == ["100.64.0.5"]
    assert _cgnat_ips("en0: inet 192.168.0.2 netmask 0xffffff00") == []
