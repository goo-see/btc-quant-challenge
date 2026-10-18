"""Read-only activity timeline + ETH-grid research summary helpers for the mobile dashboard.
No trading logic: everything is derived from persisted engine/API records."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.core.types import TradingMode
from app.database.repository import Repository

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = ROOT / "docs" / "research"
_ETH_CACHE: dict[str, Any] = {"at": 0.0, "price": None}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).isoformat()


async def activity(repo: Repository, mode: TradingMode, limit: int = 30) -> list[dict[str, Any]]:
    """LIVE/PAPER activity: activation, fills, stop ratchets (from HOLD IN_POSITION_TRAIL signals
    whose stop rose), exits, halts. Newest first."""
    items: list[dict[str, Any]] = []
    ch = await repo.get_state("challenge") or {}
    if ch.get("live_activated_at"):
        items.append(
            {
                "ts": ch["live_activated_at"],
                "kind": "LIVE_ACTIVATED",
                "title": "LIVE 자동매매 시작",
                "detail": "실제 자금 운용 시작",
                "raw": "live_activated_at",
            }
        )
    for o in await repo.recent_orders(mode, limit=limit):
        if o.executed_volume <= 0:
            continue
        side = "BUY" if o.side.value == "bid" else "SELL"
        krw = (o.executed_volume * (o.avg_fill_price or Decimal(0))).quantize(Decimal(1))
        items.append(
            {
                "ts": _iso(o.fills[0].timestamp if o.fills else o.updated_at),
                "kind": side,
                "title": f"BTC ₩{krw:,.0f} {'매수' if side == 'BUY' else '매도'}",
                "detail": f"@ ₩{(o.avg_fill_price or Decimal(0)):,.0f} · {o.executed_volume} BTC",
                "raw": f"{o.client_order_id} {o.state.value}",
            }
        )
    sigs = [s for s in await repo.recent_signals(limit=400) if s.mode == mode.value]
    sigs.sort(key=lambda s: s.id)
    last_stop: Decimal | None = None
    for s in sigs:
        if s.action == "BUY":
            last_stop = Decimal(str(s.stop_price)) if s.stop_price is not None else None
            continue
        if s.reason.startswith("IN_POSITION_TRAIL") and s.stop_price is not None:
            new = Decimal(str(s.stop_price))
            if last_stop is not None and new > last_stop:
                items.append(
                    {
                        "ts": _iso(s.ts),
                        "kind": "STOP_UP",
                        "title": "자동 매도 기준 상향",
                        "detail": f"₩{last_stop:,.0f} → ₩{new:,.0f}",
                        "raw": f"signal {s.id} {s.reason}",
                    }
                )
            last_stop = new if last_stop is None or new > last_stop else last_stop
        elif s.action == "SELL":
            items.append(
                {
                    "ts": _iso(s.ts),
                    "kind": "SELL_SIGNAL",
                    "title": "매도 신호",
                    "detail": s.reason,
                    "raw": f"signal {s.id}",
                }
            )
            last_stop = None
    for e in await repo.recent_events(limit=200):
        if e.type == "SLEEVE_TRANSFER":
            items.append(
                {
                    "ts": _iso(e.ts),
                    "kind": "SLEEVE_TRANSFER",
                    "title": "ETH 전략 자금 분리",
                    "detail": "BTC D0 장부 ₩50,000 → ETH Grid sleeve (실제 지갑 변동 없음)",
                    "raw": e.message,
                }
            )
            continue
        if e.level == "CRITICAL" or e.type in ("BOT_PAUSED", "BOT_RESUMED"):
            items.append(
                {
                    "ts": _iso(e.ts),
                    "kind": e.type,
                    "title": "시스템 알림",
                    "detail": e.message[:120],
                    "raw": e.type,
                }
            )
    items.sort(key=lambda x: str(x["ts"]), reverse=True)
    return items[:limit]


def eth_research_summary() -> dict[str, Any]:
    """Historical ETH grid research outcome from the saved artifacts (never from a live engine)."""
    port = RESEARCH / "eth_grid_portfolio.json"
    if not port.exists():
        return {"status": "NOT_RUN"}
    sel = json.loads(port.read_text()).get("selected")
    cands = []
    for k in ("G0", "G1", "G2", "G3"):
        f = RESEARCH / f"eth_grid_{k}.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text())
        full, r2, r6, cs2 = r["full"], r["recent_2y"], r["recent_6m"], r["cost_stress"]["2.0x"]
        cands.append(
            {
                "key": k,
                "description": r["params"]["description"],
                "gap_pct": r["params"].get("gap_pct"),
                "return_pct": full["total_return_pct"],
                "mdd_pct": full["max_drawdown_pct"],
                "pf": full["profit_factor"],
                "cycles": full["completed_cycles"],
                "fees": full["fees"],
                "recent_2y_return_pct": r2["total_return_pct"],
                "recent_2y_mdd_pct": r2["max_drawdown_pct"],
                "recent_6m_return_pct": r6["total_return_pct"],
                "recent_6m_mdd_pct": r6["max_drawdown_pct"],
                "cost_2x_return_pct": cs2["total_return_pct"],
                "halted": full["halted"],
                "gate_pass": r["gate"]["pass"],
                "gate_failed": r["gate"]["failed"],
            }
        )
    mdds = [c["mdd_pct"] for c in cands]
    return {
        "status": "FAILED" if sel in (None, "NONE") else "PASSED",
        "selected": sel or "NONE",
        "candidates": cands,
        "mdd_range_pct": [min(mdds), max(mdds)] if mdds else None,
        "recent_2y_worst_mdd_pct": max((c["recent_2y_mdd_pct"] for c in cands), default=None),
        "reasons_ok": [
            "횡보장에서는 반복 수익 발생 (cycle 승률 100%)",
            "최근 6개월 수익 플러스 (+3~10%, MDD 1~3%)",
        ],
        "reasons_fail": [
            "장기 하락장에서 ETH 재고 평가손실 큼",
            "MDD 12% 기준 초과 (15~26%; 최근 2Y 최악 ~45%)",
            "이미 산 ETH를 EMA100 guard가 보호하지 못함",
            "G1·G2는 재고 한도 ₩40,000 초과",
        ],
        "conclusion": "Grid 간격 문제가 아니라 하락장 재고 관리 구조가 문제였습니다.",
        "dataset": "Upbit KRW-ETH 5m 2017-09-30 → 2026-10-05 (941,921 bars)",
        "actual_eth": 0.0,
        "actual_orders": 0,
    }


async def eth_price() -> float | None:
    """Public ticker (60 s cache) for the research-grid preview — display only."""
    now = time.monotonic()
    if _ETH_CACHE["price"] is not None and now - _ETH_CACHE["at"] < 60:
        return float(_ETH_CACHE["price"])
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-ETH"})
            r.raise_for_status()
            px = float(r.json()[0]["trade_price"])
    except Exception:  # display-only; stale/None is acceptable
        return float(_ETH_CACHE["price"]) if _ETH_CACHE["price"] is not None else None
    _ETH_CACHE.update(at=now, price=px)
    return px


def grid_preview(price: float | None, gap_pct: float = 0.01, levels: int = 2) -> dict[str, Any]:
    """Illustrative level ladder of the historical candidate (G1 1.0 %) around the current price.
    Labelled as a past research simulation; nothing here is an order."""
    if price is None:
        return {"available": False}
    rows = []
    for k in range(levels, -levels - 1, -1):
        p = round(price * (1 + gap_pct) ** k)
        rows.append({"price": p, "side": "SELL" if k > 0 else "BUY" if k < 0 else "CURRENT"})
    return {
        "available": True,
        "price": price,
        "gap_pct": gap_pct,
        "levels": rows,
        "badge": "과거 연구 시뮬레이션",
        "note": "연구 후보 G1(1.0%)의 가격 레벨 예시 — 실제 주문 아님",
    }


ETH_EVENT_TITLES: dict[str, str] = {
    "ETH_LIVE_EXPERIMENT_ENABLED": "ETH LIVE EXPERIMENT 시작",
    "ETH_SEED_BUY": "ETH 초기 매수 체결",
    "ETH_GRID_BUY": "Grid 매수",
    "ETH_GRID_SELL": "Grid 매도 · Cycle 완료",
    "ETH_MDD_5": "ETH Grid MDD 5%",
    "ETH_MDD_10": "ETH Grid MDD 10%",
    "ETH_MDD_HALT": "ETH Grid MDD 중단",
    "ETH_BUY_PAUSE_DOWNTREND": "하락 추세 — Grid 매수 일시중지",
    "ETH_BUY_RESUME_TREND": "추세 회복 — Grid 매수 재개",
    "ETH_DAILY_PAUSE": "오늘 손실 한도 — Grid 매수 중지",
    "ETH_DAILY_RESUME": "Grid 매수 재개",
    "ETH_RECENTER": "Grid 기준가 재설정",
    "ETH_FROZEN": "ETH Grid FROZEN",
    "ETH_UNFROZEN": "ETH Grid FROZEN 해제",
    "ETH_RECONCILE_WARN": "ETH 잔고 대조 경고",
    "ETH_RECONCILE_DEFERRED": "ETH 잔고 대조 보류",
    "ETH_PROCESS_START": "ETH Grid 프로세스 시작",
    "ETH_PROCESS_STOP": "ETH Grid 프로세스 종료",
    "ETH_RECOVERY": "ETH 재시작 복구",
    "ETH_ORDER_NOT_FILLED": "ETH 주문 미체결",
    "ETH_PARTIAL_SELL": "ETH 부분 매도",
    "ETH_RISK_REJECTED": "ETH 주문 보류",
    "ETH_DAY_CLOSE": "ETH Grid 일일 마감",
}


async def eth_activity(eth_repo: Repository | None, limit: int = 30) -> list[dict[str, Any]]:
    """Korean ETH grid events from the grid process DB (LIVE EXPERIMENT · 실제 돈)."""
    if eth_repo is None:
        return []
    out: list[dict[str, Any]] = []
    for e in await eth_repo.recent_events(limit=200):
        if not e.type.startswith("ETH_") or e.type in ("ETH_RISK_REJECTED", "ETH_BUY_BLOCKED"):
            continue
        out.append(
            {
                "ts": _iso(e.ts),
                "kind": e.type,
                "title": ETH_EVENT_TITLES.get(e.type, e.type),
                "detail": e.message[:160],
                "raw": f"{e.type} {dict(e.payload or {}).get('client_order_id', '')}".strip(),
                "sleeve": "ETH_GRID",
            }
        )
    return out[:limit]
