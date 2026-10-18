"""macOS power / sleep policy observation via `pmset` (read-only; no system-wide changes).

MVP operating policy: external power, lid open, display sleep allowed, system sleep prevented by
`caffeinate -i` while the trader runs. Anything else is surfaced as a dashboard warning.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class PowerStatus:
    ac_power: bool | None
    battery_pct: int | None
    sleep_prevented: bool | None  # a PreventUserIdleSystemSleep assertion exists (caffeinate)
    system_sleep_min: int | None  # pmset 'sleep' value (0 = never)
    display_sleep_min: int | None
    warnings: list[str]
    raw_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    return out.decode(errors="replace")


def parse_batt(text: str) -> tuple[bool | None, int | None]:
    ac = None
    if "AC Power" in text:
        ac = True
    elif "Battery Power" in text:
        ac = False
    m = re.search(r"(\d+)%", text)
    return ac, int(m.group(1)) if m else None


def parse_settings(text: str) -> tuple[int | None, int | None]:
    sleep = disp = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "sleep":
            try:
                sleep = int(parts[1])
            except ValueError:
                pass
        if len(parts) >= 2 and parts[0] == "displaysleep":
            try:
                disp = int(parts[1])
            except ValueError:
                pass
    return sleep, disp


def parse_assertions(text: str) -> bool:
    m = re.search(r"PreventUserIdleSystemSleep\s+(\d)", text)
    return bool(m and m.group(1) == "1")


def build_status(batt: str, settings: str, assertions: str) -> PowerStatus:
    ac, pct = parse_batt(batt)
    sleep, disp = parse_settings(settings)
    prevented = parse_assertions(assertions)
    warns: list[str] = []
    if ac is False:
        warns.append("ON_BATTERY: connect external power")
    if not prevented and (sleep is None or sleep > 0):
        warns.append("SYSTEM_SLEEP_POSSIBLE: no caffeinate assertion active")
    if pct is not None and pct < 20 and ac is False:
        warns.append("BATTERY_LOW")
    return PowerStatus(ac, pct, prevented, sleep, disp, warns)


async def power_status() -> PowerStatus:
    try:
        batt, settings, assertions = await asyncio.gather(
            _run("pmset", "-g", "batt"), _run("pmset", "-g"), _run("pmset", "-g", "assertions")
        )
    except (OSError, TimeoutError) as exc:
        return PowerStatus(None, None, None, None, None, ["POWER_STATUS_UNAVAILABLE"], repr(exc))
    return build_status(batt, settings, assertions)
