"""LIVE readiness evaluation, grouped: Research / Infrastructure / Private API / Strategy PAPER / LIVE.

Three separate approvals: research_approved (harness), paper_approved (execution validation),
live_approved (explicit human decision — the only one live_guard honours). Nothing here ever
enables LIVE by itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config.live_guard import GateResult, LiveEvidence, evaluate_live_gate
from app.config.settings import Settings
from app.core.types import Timeframe, TradingMode
from app.database.engine import check_connection
from app.database.repository import Repository
from app.monitoring.network import evaluate_static_ip


@dataclass(slots=True)
class ReadinessItem:
    name: str
    status: str  # PASS | FAIL | NOT_RUN | PROGRESS | INFO
    value: str
    detail: str = ""


@dataclass(slots=True)
class ReadinessGroup:
    name: str
    status: str  # PASS | FAIL | PROGRESS | BLOCKED
    items: list[ReadinessItem] = field(default_factory=list)


@dataclass(slots=True)
class Readiness:
    live_allowed: bool
    block_reason: str | None
    groups: list[ReadinessGroup]
    items: list[ReadinessItem]  # flat view (backwards compatible)
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _group_status(items: list[ReadinessItem]) -> str:
    st = {i.status for i in items if i.status != "INFO"}
    if "FAIL" in st:
        return "FAIL"
    if "PROGRESS" in st or "NOT_RUN" in st:
        return "PROGRESS"
    if "WARN" in st:
        return "WARN"
    return "PASS"


async def gather_evidence(
    settings: Settings, repo: Repository
) -> tuple[LiveEvidence, dict[str, Any]]:
    cfg = settings.config.live_gate
    rt = await repo.get_state("runtime_PAPER") or {}
    paper_hours = float(rt.get("running_sec", 0.0)) / 3600.0
    epoch = rt.get("epoch_start")
    since = datetime.fromisoformat(epoch) if epoch else datetime(2000, 1, 1, tzinfo=UTC)
    crit = await repo.count_events(level="CRITICAL", since=since)
    se = await repo.get_state("strategy_epoch_PAPER") or {}
    se_start = datetime.fromisoformat(se["started_at"]) if se.get("started_at") else None
    paper_trades = len(await repo.closed_trades(TradingMode.PAPER, since=se_start))
    last_rec = await repo.last_reconciliation()
    order_test = await repo.get_state("order_test") or {}
    last_bt = await repo.last_backtest_ts()
    bt_days = (datetime.now(UTC) - last_bt).total_seconds() / 86400 if last_bt else None
    bt_metrics = await repo.last_backtest_metrics()
    bt_quality_ok = False
    if bt_metrics:
        pf = float(bt_metrics.get("profit_factor", 0) or 0)
        ret = float(bt_metrics.get("net_return_pct", 0) or 0)
        bt_quality_ok = (
            pf >= cfg.backtest_min_profit_factor and ret >= cfg.backtest_min_net_return_pct
        )
    ip_state = await repo.get_state("public_ip") or {}
    ip_verdict = evaluate_static_ip(
        cfg.static_public_ip or None,
        list(ip_state.get("history", [])),
        min_hours=cfg.static_ip_min_observation_hours,
    )
    # Active LIVE epoch restart context (see LiveEvidence.live_epoch_restart)
    version = settings.config.strategy.strategy_version
    st = cfg.status_of(version)
    se_live = await repo.get_state("strategy_epoch_LIVE") or {}
    challenge = await repo.get_state("challenge") or {}
    frozen = _frozen_hash(settings)
    restart_checks = {
        "live_epoch_exists": bool(se_live),
        "same_strategy_version": se_live.get("strategy_version") == version,
        "same_frozen_hash": bool(frozen) and se_live.get("parameter_hash") == frozen,
        "live_activated": bool(challenge.get("live_activated_at")),
        "live_approved": st.live_approved,
        "live_gate_enabled": cfg.enabled,
    }
    live_epoch_restart = all(restart_checks.values())
    live_epoch_detail = (
        f"epoch {se_live.get('strategy_version')}/{se_live.get('parameter_hash')} since "
        f"{str(se_live.get('started_at', ''))[:16]}, activated {str(challenge.get('live_activated_at', ''))[:16]}"
        if live_epoch_restart
        else "no active LIVE epoch: " + ", ".join(k for k, v in restart_checks.items() if not v)
    )
    ev = LiveEvidence(
        static_ip_ok=ip_verdict.passed,
        static_ip_reason=ip_verdict.reason,
        paper_hours=paper_hours,
        paper_trades=paper_trades,
        paper_critical_errors=crit,
        last_reconcile_ok=(last_rec.status == "OK") if last_rec else None,
        order_test_ok=bool(order_test.get("ok")) if order_test else None,
        days_since_last_backtest=bt_days,
        live_epoch_restart=live_epoch_restart,
        live_epoch_detail=live_epoch_detail,
        extra={"backtest_quality_ok": str(bt_quality_ok)},
    )
    raw = {
        "last_reconcile_at": last_rec.ts.isoformat() if last_rec else None,
        "backtest_metrics": bt_metrics,
        "backtest_quality_ok": bt_quality_ok,
        "backtest_last_at": last_bt.isoformat() if last_bt else None,
        "backtest_age_days": bt_days,
        "backtest_fresh": bool(bt_days is not None and bt_days <= cfg.require_recent_backtest_days),
        "live_epoch_restart": live_epoch_restart,
        "live_epoch_detail": live_epoch_detail,
        "live_epoch_checks": restart_checks,
        "order_test": order_test,
        "epoch_start": epoch,
        "static_ip": ip_verdict.to_dict(),
        "strategy_epoch": se,
        "challenge": await repo.get_state("challenge") or {},
        "exchange_balance": await repo.get_state("exchange_balance") or {},
        "replay_e2e": await repo.get_state("replay_e2e") or {},
        "restart_recovery": await repo.get_state("restart_recovery") or {},
    }
    return ev, raw


def _find(result: GateResult, name: str) -> bool | None:
    for c in result.checks:
        if c.name == name:
            return c.passed
    return None


def _step(order_test: dict[str, Any], name: str) -> tuple[str, str]:
    for st in order_test.get("steps", []):
        if st["name"] == name:
            return ("PASS" if st["ok"] else "FAIL"), str(st.get("detail", ""))
    return "NOT_RUN", "not run"


async def evaluate_readiness(settings: Settings, repo: Repository) -> Readiness:
    cfg = settings.config.live_gate
    version = settings.config.strategy.strategy_version
    st = cfg.status_of(version)
    ev, raw = await gather_evidence(settings, repo)
    result = evaluate_live_gate(settings, ev)
    ipv: dict[str, Any] = raw["static_ip"]
    ip_pass = bool(ipv["passed"])
    se: dict[str, Any] = raw["strategy_epoch"]
    counters: dict[str, Any] = se.get("counters", {})
    ot: dict[str, Any] = raw["order_test"]
    ch: dict[str, Any] = raw["challenge"]
    replay: dict[str, Any] = raw["replay_e2e"]
    rr: dict[str, Any] = raw["restart_recovery"]

    research_items = [
        ReadinessItem(
            "Strategy",
            "PASS" if st.research_approved else "FAIL",
            f"{version} {'APPROVED' if st.research_approved else 'NOT APPROVED'}",
            "walk-forward research harness",
        ),
        ReadinessItem(
            "Backtest evidence",
            "PASS"
            if (bool(raw["backtest_fresh"]) and raw["backtest_quality_ok"])
            else (
                "WARN"
                if (bool(_find(result, "recent_backtest")) and raw["backtest_quality_ok"])
                else "FAIL"
            ),
            f"recent={'yes' if raw['backtest_fresh'] else 'no'} quality={'ok' if raw['backtest_quality_ok'] else 'fail'}",
            str(raw["backtest_metrics"] or "")[:120],
        ),
        ReadinessItem(
            "Frozen-param backtest age",
            "PASS" if raw["backtest_fresh"] else "WARN",
            (
                f"{raw['backtest_age_days']:.1f}d / limit {cfg.require_recent_backtest_days}d"
                if raw["backtest_age_days"] is not None
                else "none"
            ),
            (
                f"last {str(raw['backtest_last_at'] or '')[:16]}; "
                + (
                    "fresh"
                    if raw["backtest_fresh"]
                    else (
                        "STALE — restart of the active LIVE epoch allowed (warning); a new "
                        "activation / epoch / hash change requires a fresh frozen-param backtest"
                        if raw["live_epoch_restart"]
                        else "STALE — LIVE activation blocked until a fresh frozen-param backtest is recorded"
                    )
                )
            ),
        ),
    ]
    research = ReadinessGroup(
        "Research",
        _group_status(research_items) if st.research_approved else "FAIL",
        research_items,
    )
    infra_items = [
        ReadinessItem(
            "Runtime",
            "PASS" if ev.paper_hours >= cfg.min_paper_hours else "PROGRESS",
            f"{ev.paper_hours:.1f} / {cfg.min_paper_hours:.0f}h",
            f"infrastructure epoch since {str(raw['epoch_start'] or '')[:16]}",
        ),
        ReadinessItem(
            "Critical errors",
            "PASS" if ev.paper_critical_errors <= cfg.max_paper_critical_errors else "FAIL",
            str(ev.paper_critical_errors),
            "since infrastructure epoch",
        ),
        ReadinessItem(
            "Reconciliation",
            "PASS"
            if ev.last_reconcile_ok
            else ("NOT_RUN" if ev.last_reconcile_ok is None else "FAIL"),
            str(raw["last_reconcile_at"] or "never")[:19],
        ),
        ReadinessItem(
            "Restart / recovery",
            "PASS" if rr.get("ok") else ("NOT_RUN" if not rr else "FAIL"),
            str(rr.get("summary", "not recorded")),
            str(rr.get("at", ""))[:19],
        ),
        ReadinessItem(
            "Static public IP",
            "PASS" if ip_pass else ("PROGRESS" if ipv.get("pending") else "FAIL"),
            str(ipv["latest"] or "unobserved") + f" ({ipv['span_hours']:.1f}h observed)",
            str(ipv["reason"]),
        ),
    ]
    infra = ReadinessGroup("Infrastructure", _group_status(infra_items), infra_items)
    api_items = [
        ReadinessItem(
            "Credentials",
            "PASS" if settings.env.has_credentials else "FAIL",
            "present" if settings.env.has_credentials else "missing",
        ),
        ReadinessItem("accounts", *_step(ot, "accounts")),
        ReadinessItem("orders/chance", *_step(ot, "orders_chance")),
        ReadinessItem("orders/test", *_step(ot, "orders_test")),
        ReadinessItem("Challenge capital", *_step(ot, "challenge_capital")),
    ]
    api = ReadinessGroup("Private API", _group_status(api_items), api_items)
    s_hours = float(counters.get("running_sec", 0.0)) / 3600
    min_h = cfg.strategy_paper_min_hours
    now = datetime.now(UTC)
    hs = await repo.last_health_snapshot()
    db_ok = await check_connection(repo._engine)
    trader_online = False
    bot = await repo.get_state("bot") or {}
    if bot.get("heartbeat"):
        trader_online = (
            now - datetime.fromisoformat(bot["heartbeat"])
        ).total_seconds() < settings.config.kill_switch.heartbeat_stale_sec
    ws_ok = bool(hs and hs.ws_connected and trader_online)
    rest_ok = bool(hs and hs.rest_errors == 0 and trader_online)
    _, h1_max, _ = await repo.candle_range(settings.config.system.market, Timeframe.H1)
    h1_age_h = (now - h1_max).total_seconds() / 3600 if h1_max else None
    aggr: dict[str, Any] = se.get("aggregation", {})

    def _agg_item(tf: str, max_age_h: float) -> ReadinessItem:
        a = aggr.get(tf)
        if not a:
            return ReadinessItem(
                f"{tf} aggregation",
                "PROGRESS",
                "no bar closed yet",
                "waits for the first closed bar of this epoch",
            )
        age = (now - datetime.fromisoformat(str(a["last_close"]))).total_seconds() / 3600
        return ReadinessItem(
            f"{tf} aggregation",
            "PASS" if age <= max_age_h else "FAIL",
            f"{int(a['bars'])} bars, last close {age:.1f}h ago",
        )

    frozen_hash = _frozen_hash(settings)
    ready_map: dict[str, bool] = se.get("indicators_ready", {})
    replay_ok = bool(replay.get("ok")) and replay.get("strategy_version") == version
    stop_persist = next(
        (x for x in replay.get("results", []) if str(x.get("name", "")).startswith("10_")), None
    )
    rr_after_epoch = bool(rr.get("ok")) and (
        not se.get("started_at") or str(rr.get("at", "")) >= str(se.get("started_at", ""))
    )
    strat_items = [
        ReadinessItem(
            "Strategy version",
            "INFO",
            str(se.get("strategy_version") or se.get("strategy") or "—"),
            f"hash {se.get('parameter_hash', '—')} since {str(se.get('started_at', ''))[:16]}",
        ),
        ReadinessItem(
            "Strategy runtime",
            "PASS" if s_hours >= min_h else "PROGRESS",
            f"{s_hours:.1f} / {min_h:.0f}h",
            "active strategy epoch RUNNING time",
        ),
        ReadinessItem(
            "Critical errors",
            "PASS" if int(counters.get("critical_errors", 0)) == 0 else "FAIL",
            str(int(counters.get("critical_errors", 0))),
            "since strategy epoch",
        ),
        ReadinessItem(
            "Reconciliation",
            "PASS"
            if ev.last_reconcile_ok
            else ("NOT_RUN" if ev.last_reconcile_ok is None else "FAIL"),
            str(raw["last_reconcile_at"] or "never")[:19],
        ),
        ReadinessItem(
            "WebSocket", "PASS" if ws_ok else "FAIL", "CONNECTED" if ws_ok else "DISCONNECTED"
        ),
        ReadinessItem("REST", "PASS" if rest_ok else "FAIL", "OK" if rest_ok else "ERROR/OFFLINE"),
        ReadinessItem("Database", "PASS" if db_ok else "FAIL", "OK" if db_ok else "ERROR"),
        ReadinessItem(
            "1h ingest",
            "PASS" if (h1_age_h is not None and h1_age_h <= 2.5) else "FAIL",
            f"last 1h bar {h1_age_h:.1f}h ago" if h1_age_h is not None else "no 1h bars",
            "1h bars persisted live + REST tail sync at start",
        ),
        _agg_item("4h", 4.5),
        _agg_item("1d", 25.0),
        ReadinessItem(
            "Indicator state",
            "PASS" if ready_map and all(ready_map.values()) else "PROGRESS",
            ", ".join(f"{k}:{'ready' if v else 'warming'}" for k, v in ready_map.items())
            or "unknown",
        ),
        ReadinessItem(
            "Frozen parameter hash",
            "PASS" if (frozen_hash and se.get("parameter_hash") == frozen_hash) else "FAIL",
            f"epoch {se.get('parameter_hash', '—')} vs frozen {frozen_hash or '—'}",
            str(settings.config.strategy.version_file or "no version_file"),
        ),
        ReadinessItem(
            "Restart / recovery",
            "PASS" if rr_after_epoch else ("NOT_RUN" if not rr else "FAIL"),
            str(rr.get("summary", "not recorded"))[:80],
            "must be recorded after the strategy epoch start",
        ),
        ReadinessItem(
            "Replay E2E",
            "PASS" if replay_ok else ("NOT_RUN" if not replay else "FAIL"),
            f"{replay.get('passed', 0)}/{replay.get('total', 0)} scenarios"
            if replay
            else "not run",
            f"recorded for {replay.get('strategy_version', '—')}",
        ),
        ReadinessItem(
            "Trailing stop persistence",
            "PASS"
            if (stop_persist and stop_persist.get("ok"))
            else ("NOT_RUN" if not stop_persist else "FAIL"),
            "scenario 10 (restart with open position)",
            str(stop_persist.get("detail", ""))[:80] if stop_persist else "",
        ),
        ReadinessItem(
            "Natural signals",
            "INFO",
            str(int(counters.get("buy_sell_signals", 0))),
            "recorded, not a PASS condition",
        ),
        ReadinessItem(
            "Natural trades",
            "INFO",
            f"{ev.paper_trades} (min_paper_trades {cfg.min_paper_trades} is informational)",
            "recorded, not a PASS condition",
        ),
    ]
    gate_items = [i for i in strat_items if i.status != "INFO"]
    paper_ready = all(i.status == "PASS" for i in gate_items)
    strat_items.append(
        ReadinessItem(
            "paper_approval_ready",
            "PASS" if paper_ready else "PROGRESS",
            "true" if paper_ready else "false",
            "all operational checks PASS; human sets paper_approved",
        )
    )
    strat_items.append(
        ReadinessItem(
            "PAPER approval",
            "PASS" if st.paper_approved else "FAIL",
            "YES" if st.paper_approved else "NO",
            "execution validation approval (human, never automatic)",
        )
    )
    strat = ReadinessGroup("Strategy PAPER", _group_status(strat_items), strat_items)

    live_allowed = (
        result.allowed
        and bool(raw["backtest_quality_ok"])
        and st.live_approved
        and st.paper_approved
        and st.research_approved
        and bool(ot.get("ok"))
    )
    if not st.live_approved:
        block_reason = "STRATEGY_NOT_APPROVED"
    elif not st.paper_approved:
        block_reason = "PAPER_NOT_APPROVED"
    elif not ip_pass:
        block_reason = "STATIC_PUBLIC_IP_NOT_VERIFIED"
    elif not ot.get("ok"):
        block_reason = str(ot.get("blocked_reason") or "PRIVATE_API_NOT_VERIFIED")
    elif result.failures:
        block_reason = result.failures[0].name.upper()
    elif not st.research_approved:
        block_reason = "RESEARCH_NOT_APPROVED"
    elif not live_allowed:
        block_reason = "BACKTEST_QUALITY"
    else:
        block_reason = None
    live_items = [
        ReadinessItem(
            "live_gate.enabled", "PASS" if cfg.enabled else "FAIL", str(cfg.enabled), "config gate"
        ),
        ReadinessItem(
            "LIVE approval",
            "PASS" if st.live_approved else "FAIL",
            "YES" if st.live_approved else "NO",
            "explicit human approval",
        ),
        ReadinessItem(
            "LIVE epoch restart context",
            "INFO",
            "ACTIVE_EPOCH_RESTART" if raw["live_epoch_restart"] else "PROMOTION",
            str(raw["live_epoch_detail"])[:160],
        ),
        ReadinessItem(
            "Challenge",
            "INFO",
            str(ch.get("status", "NOT_STARTED")),
            f"initial ₩{float(ch.get('initial_capital', settings.config.challenge.initial_capital_krw)):,.0f}; starts at first LIVE BUY fill",
        ),
    ]
    live = ReadinessGroup("LIVE", "PASS" if live_allowed else "BLOCKED", live_items)
    groups = [research, infra, api, strat, live]
    flat = [i for g in groups for i in g.items]
    return Readiness(live_allowed, block_reason, groups, flat, datetime.now(UTC).isoformat())


def _frozen_hash(settings: Settings) -> str | None:
    """sha256[:16] of the active strategy's parameter snapshot — same formula as the trader epoch."""
    import hashlib
    import json

    params = settings.config.strategy.params
    if not params:
        return None
    snap = {k: float(v) for k, v in sorted(params.items())}
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()[:16]
