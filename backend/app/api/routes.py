"""Read-only dashboard endpoints. Data comes from the shared DB, never from the trader process."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import get_eth_repo, get_repo, get_settings
from app.api.tailscale import tailscale_status
from app.config.settings import Settings
from app.core.state import BotState, execution_mode
from app.core.types import Timeframe, TradingMode
from app.database.engine import check_connection
from app.database.repository import Repository
from app.market.candle_engine import resample_1m
from app.monitoring.power import power_status
from app.monitoring.readiness import evaluate_readiness

router = APIRouter(prefix="/api")

RepoDep = Annotated[Repository, Depends(get_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
EthRepoDep = Annotated[Repository | None, Depends(get_eth_repo)]


def _d(x: Any) -> float | None:
    if x is None:
        return None
    with contextlib.suppress(Exception):
        return float(x)
    return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).isoformat()


def _age(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    return (now - datetime.fromisoformat(iso)).total_seconds()


async def _bot(repo: Repository) -> dict[str, Any]:
    return await repo.get_state("bot") or {}


async def _market(repo: Repository) -> dict[str, Any]:
    return await repo.get_state("market") or {}


async def _portfolio_view(settings: Settings, repo: Repository) -> dict[str, Any]:
    pf = await repo.get_state("portfolio") or {}
    st = pf.get("state") or {}
    mk = await _market(repo)
    price = Decimal(str(mk["price"])) if mk.get("price") else None
    initial = Decimal(str(st.get("initial_equity", settings.config.challenge.initial_capital_krw)))
    krw = Decimal(str(st.get("krw", initial)))
    btc = Decimal(str(st.get("btc", 0)))
    avg = Decimal(str(st["avg_entry"])) if st.get("avg_entry") else None
    entry_fees = Decimal(str(st.get("entry_fees", 0)))
    btc_value = (btc * price) if price is not None else Decimal(0)
    equity = krw + btc_value
    unrealized = (
        ((price - avg) * btc - entry_fees)
        if (price is not None and avg is not None and btc > 0)
        else Decimal(0)
    )
    realized = Decimal(str(st.get("realized_pnl", 0)))
    hwm = Decimal(str(st.get("hwm", initial)))
    dd = (hwm - equity) / hwm if hwm > 0 and equity < hwm else Decimal(0)
    daily = pf.get("daily") or {}
    day_start = Decimal(str(daily.get("start_equity", initial)))
    goal = settings.config.challenge.goal_krw
    progress = (equity - initial) / (goal - initial) if goal > initial else Decimal(0)
    return {
        "initial_equity": float(initial),
        "equity": float(equity),
        "krw": float(krw),
        "btc": float(btc),
        "btc_value": float(btc_value),
        "btc_price": _d(price),
        "avg_entry": _d(avg),
        "entry_time": st.get("entry_time"),
        "stop_price": _d(pf.get("stop_price")),
        "realized_pnl": float(realized),
        "unrealized_pnl": float(unrealized),
        "total_pnl": float(realized + unrealized),
        "total_return_pct": float((equity - initial) / initial * 100) if initial else 0.0,
        "today_pnl": float(equity - day_start),
        "today_pnl_pct": float((equity - day_start) / day_start * 100) if day_start else 0.0,
        "hwm": float(hwm),
        "drawdown_pct": float(dd * 100),
        "max_drawdown_pct": float(Decimal(str(st.get("max_drawdown", 0))) * 100),
        "fees_total": float(Decimal(str(st.get("fees_total", 0)))),
        "consecutive_losses": pf.get("consecutive_losses", 0),
        "has_position": btc > 0,
        "goal": float(goal),
        "goal_progress_pct": float(max(Decimal(0), min(Decimal(1), progress)) * 100),
        "goal_progress_raw_pct": float(progress * 100),
        "entry_reason": pf.get("entry_reason", ""),
    }


@router.get("/status")
async def status(settings: SettingsDep, repo: RepoDep, eth_repo: EthRepoDep) -> dict[str, Any]:
    now = datetime.now(UTC)
    bot = await _bot(repo)
    hb_age = _age(bot.get("heartbeat"), now)
    stale = settings.config.kill_switch.heartbeat_stale_sec
    online = hb_age is not None and hb_age < stale
    pf = await _portfolio_view(settings, repo)
    ks = await repo.get_state("kill_switch") or {}
    started = bot.get("started_at")
    warning: dict[str, Any] | None = None
    if bot.get("state") == "HALTED" and pf["has_position"]:
        warning = {
            "title": "HALTED WITH OPEN POSITION — trading disabled, no automatic liquidation",
            "halted_reason": bot.get("reason"),
            "halted_since": bot.get("since"),
            "quantity_btc": pf["btc"],
            "position_value": pf["btc_value"],
            "entry_price": pf["avg_entry"],
            "current_price": pf["btc_price"],
            "unrealized_pnl": pf["unrealized_pnl"],
            "stop_price": pf["stop_price"],
            "trading_disabled": True,
        }
    ch = await repo.get_state("challenge") or {}
    live_start = ch.get("live_start_krw")
    from app.api.eth_live import global_overview

    ov = await global_overview(settings, repo, eth_repo, pf)
    challenge_pnl = ov["total_pnl"] if live_start else None
    return {
        "overview": ov,
        "mode": bot.get("mode", settings.mode.value),
        "execution_mode": execution_mode(
            BotState(str(bot.get("state", "MAINTENANCE"))), str(bot.get("reason") or "")
        ).value
        if bot.get("state") in {s.value for s in BotState}
        else None,
        "state": bot.get("state", "UNKNOWN"),
        "state_reason": bot.get("reason"),
        "state_since": bot.get("since"),
        "online": online,
        "heartbeat_age_sec": hb_age,
        "run_id": bot.get("run_id"),
        "pid": bot.get("pid"),
        "started_at": started,
        "uptime_sec": _age(started, now) if online else None,
        "strategy": bot.get("strategy"),
        "kill_switch": ks or None,
        "halted_position_warning": warning,
        "challenge": {
            "initial": pf["initial_equity"],
            "goal": pf["goal"],
            "progress_pct": pf["goal_progress_pct"],
            "raw_pct": pf["goal_progress_raw_pct"],
            "status": ch.get("status", "NOT_STARTED"),
            "initial_challenge_capital": _d(ch.get("initial_challenge_capital")),
            "live_start_krw": _d(live_start),
            "live_activated_at": ch.get("live_activated_at"),
            "challenge_started_at": ch.get("challenge_started_at"),
            "start_date": ch.get("start_date"),
            "challenge_pnl": challenge_pnl,
            "external_flows": len(ch.get("external_flows", [])),
        },
        "server_time": now.isoformat(),
    }


@router.get("/portfolio")
async def portfolio(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    return await _portfolio_view(settings, repo)


@router.get("/stats")
async def stats(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    trades = await repo.closed_trades(mode)
    pnls = [Decimal(str(t.net_pnl)) for t in trades if t.net_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins, Decimal(0))
    gl = -sum(losses, Decimal(0))
    tz = ZoneInfo(settings.config.system.report_timezone)
    now = datetime.now(UTC)
    day_start = (
        now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    )
    week = now - timedelta(days=7)

    def _sum(since: datetime) -> float:
        return float(
            sum(
                (
                    Decimal(str(t.net_pnl))
                    for t in trades
                    if t.exit_time
                    and (
                        t.exit_time.replace(tzinfo=UTC)
                        if t.exit_time.tzinfo is None
                        else t.exit_time
                    )
                    >= since
                    and t.net_pnl is not None
                ),
                Decimal(0),
            )
        )

    return {
        "mode": mode.value,
        "trade_count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
        "average_win": float(gw / len(wins)) if wins else 0.0,
        "average_loss": float(gl / len(losses)) if losses else 0.0,
        "payoff_ratio": float((gw / len(wins)) / (gl / len(losses)))
        if wins and losses and gl > 0
        else None,
        "profit_factor": float(gw / gl) if gl > 0 else (None if not wins else float("inf")),
        "expectancy": float((gw - gl) / len(pnls)) if pnls else 0.0,
        "fees_paid": float(sum((Decimal(str(t.fees)) for t in trades), Decimal(0))),
        "realized_today": _sum(day_start),
        "realized_7d": _sum(week),
        "best_trade": float(max(pnls)) if pnls else None,
        "worst_trade": float(min(pnls)) if pnls else None,
    }


@router.get("/trades")
async def trades(
    settings: SettingsDep, repo: RepoDep, limit: int = Query(50, le=500)
) -> list[dict[str, Any]]:
    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    rows = await repo.closed_trades(mode, limit=limit)
    return [
        {
            "id": t.id,
            "entry_time": _iso(t.entry_time),
            "exit_time": _iso(t.exit_time),
            "entry_price": _d(t.entry_price),
            "exit_price": _d(t.exit_price),
            "quantity": _d(t.quantity),
            "amount": _d(Decimal(str(t.entry_price)) * Decimal(str(t.quantity))),
            "fees": _d(t.fees),
            "net_pnl": _d(t.net_pnl),
            "return_pct": _d(t.return_pct),
            "holding_sec": t.holding_sec,
            "strategy": t.strategy,
            "entry_reason": t.entry_reason,
            "exit_reason": t.exit_reason,
        }
        for t in rows
    ]


@router.get("/signals")
async def signals(repo: RepoDep, limit: int = Query(30, le=200)) -> list[dict[str, Any]]:
    rows = await repo.recent_signals(limit)
    return [
        {
            "id": s.id,
            "ts": _iso(s.ts),
            "mode": s.mode,
            "action": s.action,
            "strength": s.strength,
            "reason": s.reason,
            "timeframe": s.timeframe,
            "indicators": {
                k: round(v, 4)
                for k, v in (s.indicators or {}).items()
                if k in ("close", "ema_fast", "ema_slow", "rsi", "atr", "vol_sma")
            },
            "stop_price": _d(s.stop_price),
            "risk": await _risk_for(repo, s.id),
        }
        for s in rows
    ]


async def _risk_for(repo: Repository, signal_id: int) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.database.models import RiskDecisionRow

    async with repo._factory() as s:
        row = (
            await s.execute(
                select(RiskDecisionRow)
                .where(RiskDecisionRow.signal_id == signal_id)
                .order_by(RiskDecisionRow.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    reason = row.reason or ""
    execution = (
        "EXECUTED"
        if row.approved
        else ("SUPPRESSED" if reason.startswith("SUPPRESSED") else "BLOCKED")
    )
    return {
        "approved": row.approved,
        "reason": row.reason,
        "order_krw": _d(row.order_krw),
        "execution": execution,
    }


@router.get("/equity")
async def equity(
    settings: SettingsDep, repo: RepoDep, hours: int = Query(24 * 7, le=24 * 90)
) -> list[dict[str, Any]]:
    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    rows = await repo.snapshots(mode, since=datetime.now(UTC) - timedelta(hours=hours), limit=20000)
    return [
        {
            "time": int((r.ts.replace(tzinfo=UTC) if r.ts.tzinfo is None else r.ts).timestamp()),
            "equity": _d(r.equity),
            "drawdown": _d(r.drawdown),
            "btc_price": _d(r.btc_price),
        }
        for r in rows
    ]


@router.get("/candles")
async def candles(
    settings: SettingsDep, repo: RepoDep, tf: str = Query("15m"), limit: int = Query(300, le=2000)
) -> dict[str, Any]:
    timeframe = Timeframe(tf)
    minutes = timeframe.minutes * limit
    start = datetime.now(UTC) - timedelta(minutes=minutes)
    m1 = await repo.load_candles(settings.config.system.market, Timeframe.M1, start)
    bars = m1 if timeframe is Timeframe.M1 else resample_1m(m1, timeframe)
    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    trades_rows = await repo.closed_trades(mode, since=start)
    open_trade = await repo.get_open_trade(mode)
    markers: list[dict[str, Any]] = []
    for t in trades_rows:
        markers.append(
            {
                "time": int(
                    (
                        t.entry_time.replace(tzinfo=UTC)
                        if t.entry_time.tzinfo is None
                        else t.entry_time
                    ).timestamp()
                ),
                "side": "BUY",
                "price": _d(t.entry_price),
                "text": t.entry_reason[:24],
            }
        )
        if t.exit_time:
            markers.append(
                {
                    "time": int(
                        (
                            t.exit_time.replace(tzinfo=UTC)
                            if t.exit_time.tzinfo is None
                            else t.exit_time
                        ).timestamp()
                    ),
                    "side": "SELL",
                    "price": _d(t.exit_price),
                    "text": (t.exit_reason or "")[:24],
                }
            )
    if open_trade is not None:
        markers.append(
            {
                "time": int(
                    (
                        open_trade.entry_time.replace(tzinfo=UTC)
                        if open_trade.entry_time.tzinfo is None
                        else open_trade.entry_time
                    ).timestamp()
                ),
                "side": "BUY",
                "price": _d(open_trade.entry_price),
                "text": "OPEN " + open_trade.entry_reason[:20],
            }
        )
    return {
        "tf": tf,
        "candles": [
            {
                "time": int(c.open_time.timestamp()),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume),
            }
            for c in bars[-limit:]
        ],
        "markers": sorted(markers, key=lambda m: m["time"]),
        "open_position": (
            {"entry_price": _d(open_trade.entry_price), "quantity": _d(open_trade.quantity)}
            if open_trade
            else None
        ),
    }


@router.get("/health")
async def health(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    now = datetime.now(UTC)
    bot = await _bot(repo)
    mk = await _market(repo)
    hb_age = _age(bot.get("heartbeat"), now)
    trader_online = hb_age is not None and hb_age < settings.config.kill_switch.heartbeat_stale_sec
    hs = await repo.last_health_snapshot()
    rec = await repo.last_reconciliation()
    db_ok = await check_connection(repo._engine)
    ts_status = await tailscale_status()
    power = await power_status()
    data_age = _age(mk.get("last_data_at"), now)
    return {
        "trader": "ONLINE" if trader_online else "OFFLINE",
        "heartbeat_age_sec": hb_age,
        "uptime_sec": _age(bot.get("started_at"), now) if trader_online else None,
        "websocket": (
            "CONNECTED" if (hs and hs.ws_connected and trader_online) else "DISCONNECTED"
        ),
        "rest": ("OK" if (hs and hs.rest_errors == 0) else ("ERROR" if hs else "UNKNOWN")),
        "database": "OK" if db_ok else "ERROR",
        "tailscale": ts_status,
        "last_market_data": mk.get("last_data_at"),
        "market_data_age_sec": data_age,
        "market_price": _d(mk.get("price")),
        "api_latency_ms": hs.rest_latency_ms if hs else None,
        "reconnect_count": hs.reconnect_count if hs else None,
        "api_errors": hs.rest_errors if hs else None,
        "ws_messages": hs.msg_count if hs else None,
        "reconciliation": {
            "status": rec.status if rec else "NOT_RUN",
            "at": _iso(rec.ts) if rec else None,
            "krw_diff": _d(rec.krw_diff) if rec else None,
            "btc_diff": _d(rec.btc_diff) if rec else None,
        },
        "last_health_snapshot": _iso(hs.ts) if hs else None,
        "power": power.to_dict(),
        "state": bot.get("state"),
        "state_reason": bot.get("reason"),
    }


@router.get("/events")
async def events(repo: RepoDep, limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    rows = await repo.recent_events(limit)
    return [
        {
            "id": e.id,
            "ts": _iso(e.ts),
            "level": e.level,
            "type": e.type,
            "message": e.message,
            "payload": e.payload,
        }
        for e in rows
    ]


@router.get("/commands")
async def commands(repo: RepoDep, limit: int = Query(20, le=200)) -> list[dict[str, Any]]:
    rows = await repo.recent_commands(limit)
    return [
        {
            "command_id": c.command_id,
            "type": c.type,
            "status": c.status,
            "requested_by": c.requested_by,
            "requested_at": _iso(c.ts),
            "expires_at": _iso(c.expires_at),
            "acknowledged_at": _iso(c.acknowledged_at),
            "executed_at": _iso(c.executed_at),
            "failure_reason": c.failure_reason,
            "result": c.result,
            "audit": c.audit,
        }
        for c in rows
    ]


@router.get("/readiness")
async def readiness(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    return (await evaluate_readiness(settings, repo)).to_dict()


@router.get("/content/today")
async def content_today(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    """Day-N content payload for future SNS automation (spec §17)."""
    pf = await _portfolio_view(settings, repo)
    st = await stats(settings, repo)
    ch = await repo.get_state("challenge") or {}
    start = ch.get("start_date") or settings.config.challenge.start_date
    day_n = None
    if start:
        day_n = (
            datetime.now(ZoneInfo(settings.config.system.report_timezone)).date()
            - datetime.fromisoformat(start).date()
        ).days + 1
    return {
        "day": day_n,
        "initial": pf["initial_equity"],
        "equity": pf["equity"],
        "today_pnl": pf["today_pnl"],
        "total_pnl": pf["total_pnl"],
        "total_return_pct": pf["total_return_pct"],
        "today_pnl_pct": pf["today_pnl_pct"],
        "trades_total": st["trade_count"],
        "wins": st["wins"],
        "losses": st["losses"],
        "max_drawdown_pct": pf["max_drawdown_pct"],
        "btc_price": pf["btc_price"],
        "best_trade": st["best_trade"],
        "worst_trade": st["worst_trade"],
    }


@router.get("/reports")
async def reports(
    settings: SettingsDep, repo: RepoDep, limit: int = Query(30, le=365)
) -> list[dict[str, Any]]:
    from app.database.models import DailyStatsRow

    async with repo._factory() as s:
        rows = list(
            (
                await s.execute(
                    select(DailyStatsRow).order_by(DailyStatsRow.date.desc()).limit(limit)
                )
            ).scalars()
        )
    return [
        {
            "date": r.date,
            "mode": r.mode,
            "start_equity": _d(r.start_equity),
            "end_equity": _d(r.end_equity),
            "daily_pnl": _d(r.daily_pnl),
            "trades": r.trades,
            "wins": r.wins,
            "losses": r.losses,
            "fees": _d(r.fees),
            "max_dd": _d(r.max_dd),
            "errors": r.errors,
            "report": r.report,
        }
        for r in rows
    ]


@router.get("/reports/{day}")
async def report_markdown(settings: SettingsDep, day: str) -> dict[str, Any]:
    import re as _re

    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"date": day, "markdown": None}
    path = settings.config.system.reports_dir / f"{day}.md"
    text = await asyncio.to_thread(
        lambda: path.read_text(encoding="utf-8") if path.exists() else None
    )
    return {"date": day, "markdown": text}


@router.get("/executions")
async def executions(
    settings: SettingsDep, repo: RepoDep, limit: int = Query(30, le=200)
) -> list[dict[str, Any]]:
    """Per-fill execution quality: signal vs send vs fill price, slippage, fee, stop distances,
    exit economics (execution_metrics table)."""
    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    rows = await repo.recent_execution_metrics(mode, limit=limit)
    return [
        {
            "id": r.id,
            "mode": r.mode,
            "strategy_version": r.strategy_version,
            "client_order_id": r.client_order_id,
            "side": r.side,
            "signal_time": _iso(r.signal_time),
            "signal_price": _d(r.signal_price),
            "order_sent_at": _iso(r.order_sent_at),
            "reference_price_at_send": _d(r.reference_price_at_send),
            "actual_fill_price": _d(r.actual_fill_price),
            "slippage_bps": _d(r.slippage_bps),
            "slippage_vs_reference_bps": _d(r.slippage_vs_reference_bps),
            "fee": _d(r.fee),
            "quantity": _d(r.quantity),
            "funds": _d(r.funds),
            "fill_count": r.fill_count,
            "atr": _d(r.atr),
            "initial_stop": _d(r.initial_stop),
            "stop_distance_from_signal": _d(r.stop_distance_from_signal),
            "stop_distance_from_fill": _d(r.stop_distance_from_fill),
            "exit_reason": r.exit_reason,
            "exit_trigger_price": _d(r.exit_trigger_price),
            "realized_pnl": _d(r.realized_pnl),
            "return_pct": _d(r.return_pct),
            "holding_sec": r.holding_sec,
            "filled_at": _iso(r.filled_at),
        }
        for r in rows
    ]


@router.get("/research/e1")
async def research_e1() -> dict[str, Any]:
    """Read-only view of the D0 v2 E1 forward SHADOW experiment (research virtual trades only)."""
    import asyncio

    from app.research.e1_forward.summary import FORWARD_DB, summarize

    return await asyncio.to_thread(summarize, FORWARD_DB)


@router.get("/research/eth_grid")
async def research_eth_grid() -> dict[str, Any]:
    """Read-only: ETH grid shadow state (if any) + historical research outcome + display-only
    level preview. No real ETH orders exist anywhere in this path."""
    import asyncio

    from app.api.activity import eth_price, eth_research_summary, grid_preview
    from app.research.eth_grid.summary import SHADOW_DB, grid_summary

    shadow = await asyncio.to_thread(grid_summary, SHADOW_DB)
    hist = eth_research_summary()
    px = await eth_price()
    return {
        **shadow,
        "historical": hist,
        "preview": grid_preview(px),
        "display_status": (
            "SHADOW"
            if shadow.get("status") == "SHADOW"
            else ("RESEARCH_FAILED" if hist.get("status") == "FAILED" else "PLANNED")
        ),
    }


@router.get("/activity")
async def activity_route(
    settings: SettingsDep, repo: RepoDep, eth_repo: EthRepoDep, limit: int = Query(30, le=100)
) -> list[dict[str, Any]]:
    """Read-only activity timeline (fills, stop ratchets, signals, alerts) for the current mode."""
    from app.api.activity import activity, eth_activity

    mode = TradingMode((await _bot(repo)).get("mode") or settings.mode.value)
    items = await activity(repo, mode, limit=limit)
    items += await eth_activity(eth_repo, limit=limit)
    items.sort(key=lambda x: str(x["ts"]), reverse=True)
    return items[:limit]


@router.get("/portfolio/sleeves")
async def portfolio_sleeves(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    """Strategy sleeves: BTC_D0 (live book) + ETH_GRID (shadow) + global wallet view. Read-only."""
    import asyncio

    from app.portfolio.sleeves import sleeve_snapshot
    from app.research.eth_grid.summary import SHADOW_DB, grid_summary

    pc = settings.config.portfolio
    pf = await repo.get_state("portfolio") or {}
    st = pf.get("state") or {}
    mk = await _market(repo)
    price = Decimal(str(mk["price"])) if mk.get("price") else None
    out: dict[str, Any] = {"enabled": pc.enabled, "sleeves": {}, "global": {}}
    d0_cfg = pc.sleeves.get("BTC_D0")
    if d0_cfg is not None:
        out["sleeves"]["BTC_D0"] = sleeve_snapshot(
            "BTC_D0",
            d0_cfg,
            cash=Decimal(str(st.get("krw", 0))),
            quantity=Decimal(str(st.get("btc", 0))),
            price=price,
            realized_pnl=Decimal(str(st.get("realized_pnl", 0))),
            fees=Decimal(str(st.get("fees_total", 0))),
            entry_fees=Decimal(str(st.get("entry_fees", 0))),
            avg_entry=Decimal(str(st["avg_entry"])) if st.get("avg_entry") else None,
            max_position_pct=settings.config.risk.for_mode(settings.mode).max_position_pct,
        )
        out["sleeves"]["BTC_D0"]["stop_price"] = pf.get("stop_price")
        out["sleeves"]["BTC_D0"]["entry_time"] = st.get("entry_time")
    eth_cfg = pc.sleeves.get("ETH_GRID")
    grid = await asyncio.to_thread(grid_summary, SHADOW_DB)
    ledger = await repo.get_state("sleeve_ledger_ETH_GRID") or {}
    transfers = await repo.get_state("sleeve_transfers") or {}
    if eth_cfg is not None:
        out["sleeves"]["ETH_GRID"] = {
            "strategy_id": "ETH_GRID",
            "asset": "ETH",
            "mode": eth_cfg.mode,
            "allocated_capital": str(eth_cfg.allocated_capital),
            "ledger": ledger,
            "carve_out": transfers.get("ETH_GRID"),
            "shadow": grid,
        }
    bal = await repo.get_state("exchange_balance") or {}
    krw_actual = Decimal(str(st.get("krw", 0)))
    btc_qty = Decimal(str(st.get("btc", 0)))
    btc_value = (btc_qty * price) if price is not None else Decimal(0)
    eth_cash = Decimal(str(ledger.get("cash", "0"))) if ledger else Decimal(0)
    eth_qty = Decimal(str(ledger.get("quantity", "0"))) if ledger else Decimal(0)
    eth_px = Decimal(str(ledger.get("last_price") or "0")) if ledger else Decimal(0)
    eth_value = (eth_qty * eth_px).quantize(Decimal("0.01")) if eth_px else Decimal(0)
    live_eth = bool(ledger) and ledger.get("mode") in ("LIVE", "LIVE_EXPERIMENT")
    out["global"] = {
        "actual_krw": float(krw_actual + eth_cash),
        "actual_btc": float(btc_qty),
        "actual_eth": float(eth_qty),
        "btc_value": float(btc_value),
        "eth_value": float(eth_value),
        "total_equity": float(krw_actual + btc_value + eth_cash + eth_value),
        "total_realized": float(Decimal(str(st.get("realized_pnl", 0)))),
        "total_unrealized": float(out["sleeves"].get("BTC_D0", {}).get("unrealized_pnl", 0) or 0),
        "total_fees": float(Decimal(str(st.get("fees_total", 0)))),
        "global_mdd_pct": float(Decimal(str(st.get("max_drawdown", 0))) * 100),
        "eth_reserved_krw": float(eth_cash)
        if live_eth
        else (float(eth_cfg.allocated_capital) if eth_cfg is not None else 0.0),
        "eth_live": live_eth,
        "exchange_snapshot_at": bal.get("at"),
        "note": (
            "ETH sleeve is a LIVE EXPERIMENT ledger (real ETH + KRW) — PnL separate from BTC D0"
            if live_eth
            else "ETH sleeve = cash reservation in one KRW wallet (no real ETH yet)"
        ),
    }
    return out


@router.get("/decision")
async def decision(settings: SettingsDep, repo: RepoDep) -> dict[str, Any]:
    """Latest strategy evaluation snapshot (why HOLD/BUY/SELL) — fixed-rule thresholds only."""
    bot = await _bot(repo)
    mode = bot.get("mode") or settings.mode.value
    snap = await repo.get_state(f"decision_{mode}") or {}
    mk = await _market(repo)
    tf = str(snap.get("timeframe") or settings.config.strategy.timeframe)
    minutes = Timeframe(tf).minutes if tf in {t.value for t in Timeframe} else 240
    bar_time = snap.get("market_bar_time")
    nxt = None
    if bar_time:
        nxt = (datetime.fromisoformat(str(bar_time)) + timedelta(minutes=minutes)).isoformat()
    return {
        **snap,
        "current_price": _d(mk.get("price")),
        "next_evaluation": nxt,
        "bot_state": bot.get("state"),
        "state_reason": bot.get("reason"),
    }


@router.get("/eth/live")
async def eth_live(settings: SettingsDep, eth_repo: EthRepoDep) -> dict[str, Any]:
    """ETH grid LIVE EXPERIMENT (real money, ₩50,000 sleeve): status, performance book (GRID TOTAL
    EQUITY first), live grid ladder, recent fills. Read-only on the grid process's own DB."""
    from app.api.eth_live import eth_live_view

    return await eth_live_view(eth_repo, settings)


@router.get("/overview")
async def overview(settings: SettingsDep, repo: RepoDep, eth_repo: EthRepoDep) -> dict[str, Any]:
    """Whole-account view (D0 book + ETH grid sleeve), PnL separated per sleeve."""
    from app.api.eth_live import global_overview

    pf = await _portfolio_view(settings, repo)
    return await global_overview(settings, repo, eth_repo, pf)
