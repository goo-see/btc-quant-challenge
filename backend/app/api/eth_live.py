"""Read-only views of the ETH grid LIVE EXPERIMENT (own DB) and the global two-sleeve overview.
Never touches the exchange; everything is derived from persisted engine state and order rows."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.config.settings import Settings
from app.core.types import TradingMode
from app.database.repository import Repository
from app.eth_grid.compounding import CompoundingPolicy
from app.eth_grid.live_engine import Cycle, LiveGridEngine, LiveGridState, live_params
from app.eth_grid.service import CONTROL_KEY, CYCLES_KEY, STATE_KEY

WARNING_KO = (
    "실전 실험 단계입니다. Historical Grid v1은 장기 하락장에서 큰 재고 평가손실이 확인됐습니다."
)
STATUS_KO = {
    "WAITING_ENABLE": "활성화 대기",
    "SEEDING": "초기 매수 중",
    "RUNNING": "운용 중",
    "BUY_PAUSED": "매수 일시중지",
    "HALTED": "MDD 중단 (매도만)",
    "FROZEN": "FROZEN (자동 주문 정지)",
}


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _age(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


async def eth_live_view(eth_repo: Repository | None, settings: Settings) -> dict[str, Any]:
    now = datetime.now(UTC)
    sl = settings.config.portfolio.sleeves.get("ETH_GRID")
    base: dict[str, Any] = {
        "display_status": "LIVE_EXPERIMENT",
        "badge": "LIVE EXPERIMENT · 실제 돈",
        "warning": WARNING_KO,
        "sleeve_mode": sl.mode if sl else None,
        "allocated_capital": float(sl.allocated_capital) if sl else 0.0,
        "strategy": "AGGRESSIVE V2 0.50% (new buys) · legacy G0 0.75% lots EXIT_ONLY",
        "research_gate": "FAILED",
        "server_time": now.isoformat(),
    }
    if eth_repo is None:
        return {**base, "status": "NOT_STARTED", "status_ko": "미시작", "online": False}
    st = await eth_repo.get_state(STATE_KEY) or {}
    if not st or "cash" not in st:
        return {**base, "status": "NOT_STARTED", "status_ko": "미시작", "online": False}
    ctl = await eth_repo.get_state(CONTROL_KEY) or {}
    cyc_doc = await eth_repo.get_state(CYCLES_KEY) or {}
    pol_raw = st.get("compounding_policy")
    policy = CompoundingPolicy.from_dict(pol_raw) if pol_raw else None
    engine = LiveGridEngine(
        live_params(str(st.get("candidate") or "G0")), LiveGridState.from_dict(st), policy=policy
    )
    engine.cycles = [Cycle.from_dict(c) for c in cyc_doc.get("cycles", [])]
    health = dict(st.get("health") or {})
    hb_age = _age(health.get("heartbeat"), now)
    stale = settings.config.kill_switch.heartbeat_stale_sec
    online = hb_age is not None and hb_age < stale
    px = Decimal(str(health.get("last_price") or st.get("last_price") or "0"))
    book = engine.book(px)
    ladder = engine.ladder(px) if px > 0 else {"sells": [], "buys": [], "price": "0"}
    status = str(st.get("status") or engine.status.value)
    if not online:
        status_ko = "프로세스 오프라인"
    elif engine.s.buy_pause_reason and status == "BUY_PAUSED":
        status_ko = "매수 일시중단 · 매도 유지"
    else:
        status_ko = STATUS_KO.get(status, status)
    s = engine.s
    reasons: list[str] = []
    if s.frozen:
        reasons.append(f"FROZEN: {s.frozen_reason}")
    if s.halted:
        reasons.append(f"MDD 중단: {s.halt_reason}")
    if not s.trend_ok:
        reasons.append("일봉 종가 < EMA100 (신규 매수 일시중지)")
    if s.day_paused:
        reasons.append("오늘 손실 -5% (오늘 매수 중지)")
    if s.sizing_paused_reason:
        reasons.append("복리 기준 자본이 최소 주문(₩5,000) 미만 — 신규 매수 중지")
    if s.buy_pause_reason:
        reasons.append(
            f"신규 매수 일시중단 ({s.buy_pause_reason}) — 보유 ETH는 목표가 도달 시 자동 매도"
        )
    nearest = min((lot.target for lot in s.lots), default=None)
    fills = await _recent_fills(eth_repo, engine, settings.config.eth_grid.market)
    return {
        **base,
        "status": status,
        "status_ko": status_ko,
        "status_reasons": reasons,
        "online": online,
        "heartbeat_age_sec": hb_age,
        "enabled": bool(ctl.get("enabled", False)),
        "enabled_at": ctl.get("enabled_at"),
        "pid": health.get("pid"),
        "run_id": health.get("run_id"),
        "ws_connected": health.get("ws_connected"),
        "last_tick_at": health.get("last_tick_at"),
        "errors": health.get("errors"),
        "last_error": health.get("last_error"),
        "carve_out_done": health.get("carve_out_done"),
        "trend": health.get("trend"),
        "reconcile": st.get("reconcile"),
        "candidate": st.get("candidate"),
        "params": st.get("params"),
        "price": _f(px) if px > 0 else None,
        "anchor": _f(s.anchor) if s.anchor else None,
        "gap_pct": _f(s.gap),
        "seeded_at": s.seeded_at.isoformat() if s.seeded_at else None,
        "seed_price": _f(s.seed_price),
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "pending": s.pending.to_dict() if s.pending else None,
        "book": {
            k: (_f(v) if isinstance(v, str) and k not in ("status",) else v)
            for k, v in book.items()
        },
        "ladder": ladder,
        "lots": [lot.to_dict() for lot in sorted(s.lots, key=lambda lot: lot.target)],
        "recent_fills": fills,
        "cycles_recent": [c.to_dict() for c in engine.cycles[-20:]][::-1],
        "buy_pause": {
            "paused": bool(s.buy_pause_reason),
            "reason": s.buy_pause_reason,
            "at": s.buy_pause_at.isoformat() if s.buy_pause_at else None,
            "by": s.buy_pause_by,
            "sell_enabled": not s.frozen,
            "nearest_sell_target": _f(nearest),
            "nearest_sell_distance_pct": (
                _f((nearest - px) / px * 100) if (nearest is not None and px > 0) else None
            ),
            "inventory_ratio_pct": _f(s.inventory_value(px) / s.equity(px) * 100)
            if px > 0 and s.equity(px) > 0
            else None,
            "cash_ratio_pct": _f(s.cash / s.equity(px) * 100)
            if px > 0 and s.equity(px) > 0
            else None,
            "open_lots": len(s.lots),
        },
        "active_strategy": {
            **(st.get("active_strategy") or {}),
            "epoch": s.strategy_epoch,
            "gap_pct": _f(s.gap),
            "label": (
                f"AGGRESSIVE V2 {float(s.gap) * 100:.2f}%"
                if s.strategy_epoch.startswith("AGGRESSIVE_V2")
                else f"{s.strategy_epoch} {float(s.gap) * 100:.2f}%"
            ),
            "sell_target_rule": "actual fill x (1+gap), tick-rounded up"
            if s.strategy_epoch.startswith("AGGRESSIVE_V2")
            else "level x (1+gap)",
        },
        "legacy": {
            "lots": len([lot for lot in s.lots if lot.exit_only]),
            "label": next((lot.strategy_epoch for lot in s.lots if lot.exit_only), None),
            "gap_pct": _f(next((lot.gap_pct for lot in s.lots if lot.exit_only), None)),
            "mode": "EXIT_ONLY",
        },
        "cutover_history": s.cutover_history,
        "compounding": engine.compounding_view(px),
        "limits": {
            "unit_krw": _f(engine.current_unit),
            "base_unit_krw": _f(engine.p.unit_krw),
            "max_inventory_krw": _f(engine.effective_limits(px)[0]),
            "min_cash_krw": _f(engine.effective_limits(px)[1]),
            "daily_loss_pause_pct": _f(engine.p.daily_loss_pause_pct),
            "mdd_halt_pct": _f(engine.p.mdd_halt_pct),
            "ema_period": engine.p.ema_period,
            "levels_each_side": engine.p.levels_each_side,
        },
        "min_total": _f(st.get("min_total")),
        "fee_rate": _f(st.get("fee_rate")),
        "updated_at": st.get("updated_at"),
    }


async def _recent_fills(
    repo: Repository, engine: LiveGridEngine, market: str, limit: int = 30
) -> list[dict[str, Any]]:
    by_sell = {c.sell_client_id: c for c in engine.cycles}
    out: list[dict[str, Any]] = []
    for o in await repo.recent_orders_with_fills(TradingMode.LIVE, limit=limit, market=market):
        if o.executed_volume <= 0:
            continue
        funds = sum((f.funds for f in o.fills), Decimal(0)) or (
            o.executed_volume * (o.avg_fill_price or Decimal(0))
        )
        ts = o.fills[-1].timestamp if o.fills else o.updated_at
        side = "BUY" if o.side.value == "bid" else "SELL"
        cyc = by_sell.get(o.client_order_id)
        out.append(
            {
                "ts": ts.isoformat(),
                "side": side,
                "kind": "SEED" if "seed" in o.client_order_id else "GRID",
                "krw": _f(funds),
                "price": _f(o.avg_fill_price),
                "quantity": _f(o.executed_volume),
                "fee": _f(o.paid_fee),
                "net_pnl": _f(cyc.net_pnl) if cyc else None,
                "client_order_id": o.client_order_id,
                "uuid": o.exchange_uuid,
                "state": o.state.value,
            }
        )
    return out


async def global_overview(
    settings: Settings, repo: Repository, eth_repo: Repository | None, d0_view: dict[str, Any]
) -> dict[str, Any]:
    """Whole-account view: D0 book + ETH grid book, PnL kept separate per sleeve."""
    eth = await eth_live_view(eth_repo, settings)
    transfers = await repo.get_state("sleeve_transfers") or {}
    carved = (transfers.get("ETH_GRID") or {}).get("status") == "DONE"
    sl = settings.config.portfolio.sleeves.get("ETH_GRID")
    alloc = float(sl.allocated_capital) if sl else 0.0
    book = eth.get("book") or {}
    eth_started = eth.get("status") not in (None, "NOT_STARTED")
    eth_equity = (
        float(book.get("current_equity") or alloc) if eth_started else (alloc if carved else 0.0)
    )
    eth_cash = (
        float(book.get("krw_inventory") or alloc) if eth_started else (alloc if carved else 0.0)
    )
    eth_value = float(book.get("eth_inventory_value") or 0.0) if eth_started else 0.0
    eth_today = float(book.get("today_pnl") or 0.0) if eth_started else 0.0
    eth_pnl = eth_equity - alloc if (eth_started or carved) else 0.0
    d0_equity = float(d0_view["equity"])
    total = d0_equity + eth_equity
    ch = await repo.get_state("challenge") or {}
    start = float(ch.get("live_start_krw") or settings.config.challenge.initial_capital_krw)
    d0_pnl = d0_equity - float(d0_view["initial_equity"])
    return {
        "total_equity": total,
        "total_krw": float(d0_view["krw"]) + eth_cash,
        "btc_value": float(d0_view["btc_value"]),
        "eth_value": eth_value,
        "challenge_start": start,
        "total_pnl": total - start,
        "total_pnl_pct": (total - start) / start * 100 if start else 0.0,
        "today_pnl": float(d0_view["today_pnl"]) + eth_today,
        "d0": {
            "equity": d0_equity,
            "pnl": d0_pnl,
            "today_pnl": float(d0_view["today_pnl"]),
            "allocated": float(settings.config.portfolio.sleeves["BTC_D0"].allocated_capital)
            if "BTC_D0" in settings.config.portfolio.sleeves
            else None,
            "carve_out": transfers.get("ETH_GRID"),
        },
        "eth": {
            "equity": eth_equity,
            "cash": eth_cash,
            "value": eth_value,
            "pnl": eth_pnl,
            "today_pnl": eth_today,
            "allocated": alloc,
            "status": eth.get("status"),
            "status_ko": eth.get("status_ko"),
            "online": eth.get("online"),
            "started": eth_started,
            "carved": carved,
        },
        "server_time": datetime.now(UTC).isoformat(),
    }
