from __future__ import annotations

from app.monitoring.power import build_status

BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
BATT_BAT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t15%; discharging; 1:10 remaining present: true\n"
SETTINGS = "System-wide power settings:\nCurrently in use:\n disksleep 10\n sleep 1 (sleep prevented by caffeinate)\n displaysleep 10\n"
ASSERT_ON = (
    "Assertion status system-wide:\n   PreventUserIdleSystemSleep     1\n   PreventSystemSleep 0\n"
)
ASSERT_OFF = "Assertion status system-wide:\n   PreventUserIdleSystemSleep     0\n"


def test_policy_ok_on_ac_with_caffeinate() -> None:
    s = build_status(BATT_AC, SETTINGS, ASSERT_ON)
    assert s.ac_power is True and s.battery_pct == 100 and s.sleep_prevented is True
    assert s.system_sleep_min == 1 and s.display_sleep_min == 10 and s.warnings == []


def test_warnings_on_battery_and_without_assertion() -> None:
    s = build_status(BATT_BAT, SETTINGS, ASSERT_OFF)
    assert s.ac_power is False and s.sleep_prevented is False
    assert {
        "ON_BATTERY: connect external power",
        "SYSTEM_SLEEP_POSSIBLE: no caffeinate assertion active",
        "BATTERY_LOW",
    } == set(s.warnings)
