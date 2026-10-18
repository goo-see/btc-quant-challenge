from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.config.live_guard import (
    LIVE_CONFIRM_PHRASE,
    LiveEvidence,
    assert_mode_is_safe,
    evaluate_live_gate,
)
from app.config.settings import EnvSettings, Settings, StrategyStatus, load_yaml_config
from app.core.types import TradingMode

CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.yaml"


def _settings(monkeypatch: pytest.MonkeyPatch, mode: str, **env: str) -> Settings:
    monkeypatch.setenv("TRADING_MODE", mode)
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "LIVE_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_yaml_config(CONFIG)
    # tests pin the baseline strategy regardless of which strategy is promoted in config.yaml
    cfg.strategy.version_file = None
    cfg.strategy.name = "trend_momentum"
    cfg.strategy.version = ""
    return Settings(EnvSettings(_env_file=None), cfg)


def test_config_yaml_loads_and_live_exposure_is_capped() -> None:
    cfg = load_yaml_config(CONFIG)
    live = cfg.risk.for_mode(TradingMode.LIVE)
    assert (
        live.max_position_pct <= Decimal("0.30")
        and live.max_order_krw <= 30000
        and live.max_concurrent_positions == 1
    )
    # live_gate.enabled is an operator decision (true since the LIVE approval on 2026-09-30);
    # the invariant is the exposure cap, not the flag value
    assert isinstance(cfg.live_gate.enabled, bool)
    assert cfg.risk.min_order_krw == 5000


def test_mode_parsing_case_insensitive_and_invalid_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch, "paper").mode is TradingMode.PAPER
    monkeypatch.setenv("TRADING_MODE", "YOLO")
    with pytest.raises(ValueError):
        EnvSettings(_env_file=None)


def test_live_refused_without_all_three_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        "LIVE",
        UPBIT_ACCESS_KEY="a",
        UPBIT_SECRET_KEY="b",
        LIVE_CONFIRM=LIVE_CONFIRM_PHRASE,
    )
    s.config.live_gate.enabled = False  # pin: the production yaml may be LIVE-approved
    r = evaluate_live_gate(s)
    assert not r.allowed
    # enabled false AND baseline strategy (trend_momentum) not approved
    assert [c.name for c in r.failures] == ["strategy_approved", "config_live_enabled"]
    with pytest.raises(PermissionError, match="LIVE_BLOCKED reason=STRATEGY_NOT_APPROVED"):
        assert_mode_is_safe(s)


def test_live_blocked_when_only_strategy_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Infrastructure gates all green, strategy not approved -> still blocked."""
    s = _settings(
        monkeypatch,
        "LIVE",
        UPBIT_ACCESS_KEY="a",
        UPBIT_SECRET_KEY="b",
        LIVE_CONFIRM=LIVE_CONFIRM_PHRASE,
    )
    s.config.live_gate.enabled = True
    r = evaluate_live_gate(s)
    assert [c.name for c in r.failures] == ["strategy_approved"]
    with pytest.raises(PermissionError, match="STRATEGY_NOT_APPROVED"):
        assert_mode_is_safe(s)
    s.config.live_gate.strategy_status["trend_momentum_v1"] = StrategyStatus(
        research_approved=True, paper_approved=True, live_approved=True
    )
    assert evaluate_live_gate(s).allowed


def test_live_refused_on_wrong_confirm_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch, "LIVE", UPBIT_ACCESS_KEY="a", UPBIT_SECRET_KEY="b", LIVE_CONFIRM="yes"
    )
    s.config.live_gate.enabled = True
    s.config.live_gate.strategy_status["trend_momentum_v1"] = StrategyStatus(
        research_approved=True, paper_approved=True, live_approved=True
    )
    r = evaluate_live_gate(s)
    assert [c.name for c in r.failures] == ["env_live_confirm"]


def test_paper_mode_never_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_mode_is_safe(_settings(monkeypatch, "PAPER"))


def test_evidence_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        "LIVE",
        UPBIT_ACCESS_KEY="a",
        UPBIT_SECRET_KEY="b",
        LIVE_CONFIRM=LIVE_CONFIRM_PHRASE,
    )
    s.config.live_gate.enabled = True
    s.config.live_gate.strategy_status["trend_momentum_v1"] = StrategyStatus(
        research_approved=True, paper_approved=True, live_approved=True
    )
    weak = LiveEvidence(
        static_ip_ok=True,
        paper_hours=100,
        paper_trades=3,
        paper_critical_errors=1,
        last_reconcile_ok=True,
        order_test_ok=True,
        days_since_last_backtest=1,
    )
    r = evaluate_live_gate(s, weak)
    assert {c.name for c in r.failures} == {"paper_critical_errors"}  # paper_trades informational
    strong = LiveEvidence(
        static_ip_ok=True,
        paper_hours=100,
        paper_trades=20,
        paper_critical_errors=0,
        last_reconcile_ok=True,
        order_test_ok=True,
        days_since_last_backtest=1,
    )
    assert evaluate_live_gate(s, strong).allowed
    no_ip = LiveEvidence(
        static_ip_ok=False,
        paper_hours=100,
        paper_trades=20,
        paper_critical_errors=0,
        last_reconcile_ok=True,
        order_test_ok=True,
        days_since_last_backtest=1,
    )
    r = evaluate_live_gate(s, no_ip)
    assert [c.name for c in r.failures] == ["static_public_ip"]
    assert "STATIC_PUBLIC_IP_NOT_VERIFIED" in r.failures[0].detail
