"""Every replay scenario must pass individually (pytest) — the script records the same suite as evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.validation.replay_scenarios import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario(name: str, tmp_path: Path) -> None:
    detail = await SCENARIOS[name](tmp_path)
    assert detail
