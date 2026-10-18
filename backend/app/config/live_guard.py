"""LIVE promotion gate. This is the ONLY place that can authorize real-money trading.

Static gates (all required):
  0. strategy listed in live_gate.approved_strategies (strategy readiness != infra readiness)
  1. TRADING_MODE=LIVE          (environment)
  2. live_gate.enabled: true    (config.yaml)
  3. LIVE_CONFIRM exact phrase  (environment)

Plus runtime evidence gates evaluated against DB history and the exchange:
  paper hours, paper trade count, critical errors, reconciliation, order-test, recent backtest.
Implementation order: BACKTEST -> PAPER -> /orders/test -> Small LIVE -> LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import Settings
from app.core.types import TradingMode

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_KRW"


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class LiveEvidence:
    """Runtime evidence gathered by the trader before a LIVE start (Day 4 fills these)."""

    paper_hours: float = 0.0
    paper_trades: int = 0
    paper_critical_errors: int = 0
    last_reconcile_ok: bool | None = None
    order_test_ok: bool | None = None
    days_since_last_backtest: float | None = None
    static_ip_ok: bool | None = None
    static_ip_reason: str = ""
    # Restart of an ALREADY ACTIVATED LIVE epoch (same strategy version + frozen hash,
    # live_activated_at present, approvals unchanged): the recent-backtest gate must not stop the
    # process from recovering its position — it degrades to a WARNING. A first activation, a new
    # LIVE epoch or a strategy/hash change is a PROMOTION and requires a recent backtest again.
    live_epoch_restart: bool = False
    live_epoch_detail: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    checks: tuple[GateCheck, ...]

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]


def static_gates(settings: Settings) -> list[GateCheck]:
    env, cfg = settings.env, settings.config.live_gate
    version = settings.config.strategy.strategy_version
    st = cfg.status_of(version)
    strategy_ok = st.live_approved
    return [
        GateCheck(
            "strategy_approved",
            strategy_ok,
            f"{version} live_approved"
            if strategy_ok
            else f"STRATEGY_NOT_APPROVED ({version}: research={st.research_approved} "
            f"paper={st.paper_approved} live={st.live_approved})",
        ),
        GateCheck(
            "env_trading_mode",
            env.trading_mode is TradingMode.LIVE,
            f"TRADING_MODE={env.trading_mode}",
        ),
        GateCheck("config_live_enabled", cfg.enabled, f"live_gate.enabled={cfg.enabled}"),
        GateCheck(
            "env_live_confirm",
            env.live_confirm == LIVE_CONFIRM_PHRASE,
            "LIVE_CONFIRM matches"
            if env.live_confirm == LIVE_CONFIRM_PHRASE
            else "LIVE_CONFIRM missing/mismatch",
        ),
        GateCheck(
            "credentials_present",
            env.has_credentials,
            "API keys present" if env.has_credentials else "API keys missing",
        ),
    ]


def evidence_gates(settings: Settings, ev: LiveEvidence) -> list[GateCheck]:
    cfg = settings.config.live_gate
    checks = [
        GateCheck(
            "paper_hours",
            ev.paper_hours >= cfg.min_paper_hours,
            f"{ev.paper_hours:.1f}h >= {cfg.min_paper_hours}h",
        ),
        # min_paper_trades is INFORMATIONAL for low-frequency strategies (config comment, PAPER
        # gate decision 2026-09-25): recorded in readiness, never a PASS condition.
        GateCheck(
            "paper_trades_recorded",
            True,
            f"{ev.paper_trades} (min_paper_trades {cfg.min_paper_trades} informational)",
        ),
        GateCheck(
            "paper_critical_errors",
            ev.paper_critical_errors <= cfg.max_paper_critical_errors,
            f"{ev.paper_critical_errors} <= {cfg.max_paper_critical_errors}",
        ),
    ]
    if cfg.require_reconcile_ok:
        checks.append(
            GateCheck(
                "reconcile_ok",
                ev.last_reconcile_ok is True,
                f"last_reconcile_ok={ev.last_reconcile_ok}",
            )
        )
    checks.append(
        GateCheck(
            "static_public_ip",
            ev.static_ip_ok is True,
            ev.static_ip_reason or "STATIC_PUBLIC_IP_NOT_VERIFIED",
        )
    )
    if cfg.require_order_test_ok:
        checks.append(
            GateCheck(
                "order_test_ok", ev.order_test_ok is True, f"order_test_ok={ev.order_test_ok}"
            )
        )
    if cfg.require_recent_backtest_days > 0:
        fresh = (
            ev.days_since_last_backtest is not None
            and ev.days_since_last_backtest <= cfg.require_recent_backtest_days
        )
        age = (
            f"{ev.days_since_last_backtest:.1f}d"
            if ev.days_since_last_backtest is not None
            else "none"
        )
        if fresh:
            detail = f"days_since_last_backtest={age} <= {cfg.require_recent_backtest_days}d"
        elif ev.live_epoch_restart:
            detail = (
                f"WARNING: frozen-param backtest stale ({age} > "
                f"{cfg.require_recent_backtest_days}d) — allowed only because this is a restart "
                f"of the active LIVE epoch ({ev.live_epoch_detail}); a new activation, epoch or "
                "strategy/hash change requires a fresh backtest"
            )
        else:
            detail = (
                f"days_since_last_backtest={age} > {cfg.require_recent_backtest_days}d "
                "(promotion gate: fresh backtest required)"
            )
        checks.append(GateCheck("recent_backtest", fresh or ev.live_epoch_restart, detail))
    return checks


def evaluate_live_gate(settings: Settings, evidence: LiveEvidence | None = None) -> GateResult:
    checks = static_gates(settings)
    if evidence is not None:
        checks.extend(evidence_gates(settings, evidence))
    return GateResult(allowed=all(c.passed for c in checks), checks=tuple(checks))


def assert_mode_is_safe(settings: Settings) -> None:
    """Fail fast at process start if the requested mode is not authorized."""
    if settings.mode is not TradingMode.LIVE:
        return
    result = evaluate_live_gate(settings)
    if not result.allowed:
        failed = ", ".join(f"{c.name}({c.detail})" for c in result.failures)
        first = result.failures[0]
        reason = (
            "STRATEGY_NOT_APPROVED" if first.name == "strategy_approved" else first.name.upper()
        )
        raise PermissionError(f"LIVE_BLOCKED reason={reason} failures=[{failed}]")
