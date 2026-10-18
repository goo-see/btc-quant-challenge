"""Daily report (KST boundary): aggregates DB history into daily_stats and reports/YYYY-MM-DD.md.

No LLM. Called by the trader at each KST day rollover for the previous day and by
scripts/daily_report.py on demand.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.types import TradingMode
from app.database.engine import session_scope
from app.database.models import DailyStatsRow
from app.database.repository import Repository


@dataclass(slots=True)
class DailyReport:
    date: str
    day_number: int | None
    mode: str
    start_equity: Decimal
    end_equity: Decimal
    daily_pnl: Decimal
    daily_return_pct: Decimal
    total_return_pct: Decimal
    hwm: Decimal
    max_drawdown_pct: Decimal
    trades: int
    wins: int
    losses: int
    win_rate: float
    fees: Decimal
    best_trade: Decimal | None
    worst_trade: Decimal | None
    position: dict[str, Any] | None
    bot_state: str
    bot_reason: str
    reconciliation: str
    critical_errors: int
    initial_equity: Decimal

    def to_markdown(self) -> str:
        if self.position:
            stop = self.position["stop"] or 0
            pos = (
                f"{self.position['btc']} BTC @ ₩{self.position['avg_entry']:,.0f} "
                f"(stop ₩{stop:,.0f})"
            )
        else:
            pos = "none"
        day = f"DAY {self.day_number}" if self.day_number is not None else "DAY —"
        best = f"₩{self.best_trade:,.0f}" if self.best_trade is not None else "—"
        worst = f"₩{self.worst_trade:,.0f}" if self.worst_trade is not None else "—"
        wr = self.win_rate * 100
        lines = [
            f"# {self.date} — {day} ({self.mode})",
            "",
            "| | |",
            "|---|---|",
            f"| Starting equity | ₩{self.start_equity:,.0f} |",
            f"| Ending equity | ₩{self.end_equity:,.0f} |",
            f"| Daily PnL | ₩{self.daily_pnl:,.0f} ({self.daily_return_pct:+.2f}%) |",
            f"| Total return | {self.total_return_pct:+.2f}% (from ₩{self.initial_equity:,.0f}) |",
            f"| High water mark | ₩{self.hwm:,.0f} |",
            f"| Max drawdown | {self.max_drawdown_pct:.2f}% |",
            f"| Trades | {self.trades} (W {self.wins} / L {self.losses}, win rate {wr:.1f}%) |",
            f"| Fees | ₩{self.fees:,.0f} |",
            f"| Best / Worst trade | {best} / {worst} |",
            f"| Current position | {pos} |",
            f"| Bot state | {self.bot_state} ({self.bot_reason}) |",
            f"| Trading mode | {self.mode} |",
            f"| Reconciliation | {self.reconciliation} |",
            f"| Critical errors | {self.critical_errors} |",
            "",
            "_₩1,000,000 is a content goal, not a guaranteed return._",
        ]
        return "\n".join(lines) + "\n"


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def build_daily_report(
    repo: Repository,
    *,
    mode: TradingMode,
    day: date,
    tz: str,
    initial_equity: Decimal,
    challenge_start: date | None,
) -> DailyReport:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    end = start + timedelta(days=1)
    snaps = await repo.snapshots(mode, since=start - timedelta(days=30), limit=100000)
    before = [s for s in snaps if _aware(s.ts) < start]
    inday = [s for s in snaps if start <= _aware(s.ts) < end]
    pf = await repo.get_state("portfolio") or {}
    st = pf.get("state") or {}
    start_eq = (
        Decimal(str(before[-1].equity))
        if before
        else (Decimal(str(inday[0].equity)) if inday else initial_equity)
    )
    end_eq = Decimal(str(inday[-1].equity)) if inday else start_eq
    hwm = max(
        [Decimal(str(s.hwm)) for s in inday], default=Decimal(str(st.get("hwm", initial_equity)))
    )
    mdd = Decimal(str(st.get("max_drawdown", 0))) * 100
    trades = [
        t
        for t in await repo.closed_trades(mode)
        if t.exit_time and start <= _aware(t.exit_time) < end
    ]
    pnls = [Decimal(str(t.net_pnl)) for t in trades if t.net_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    fees = sum((Decimal(str(t.fees)) for t in trades), Decimal(0))
    bot = await repo.get_state("bot") or {}
    rec = await repo.last_reconciliation()
    crit = await repo.count_events(level="CRITICAL", since=start)
    position = None
    if Decimal(str(st.get("btc", 0))) > 0:
        position = {
            "btc": st.get("btc"),
            "avg_entry": float(st.get("avg_entry") or 0),
            "stop": float(pf["stop_price"]) if pf.get("stop_price") else None,
        }
    day_number = (day - challenge_start).days + 1 if challenge_start else None
    return DailyReport(
        date=day.isoformat(),
        day_number=day_number,
        mode=mode.value,
        start_equity=start_eq,
        end_equity=end_eq,
        daily_pnl=end_eq - start_eq,
        daily_return_pct=((end_eq - start_eq) / start_eq * 100) if start_eq else Decimal(0),
        total_return_pct=((end_eq - initial_equity) / initial_equity * 100)
        if initial_equity
        else Decimal(0),
        hwm=hwm,
        max_drawdown_pct=mdd,
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(pnls)) if pnls else 0.0,
        fees=fees,
        best_trade=max(pnls) if pnls else None,
        worst_trade=min(pnls) if pnls else None,
        position=position,
        bot_state=str(bot.get("state", "UNKNOWN")),
        bot_reason=str(bot.get("reason", "")),
        reconciliation=(rec.status if rec else "NOT_RUN"),
        critical_errors=crit,
        initial_equity=initial_equity,
    )


async def save_daily_report(repo: Repository, report: DailyReport, reports_dir: Path) -> Path:
    async def _do() -> None:
        async with session_scope(repo._factory) as s:
            row = (
                await s.execute(
                    select(DailyStatsRow).where(
                        DailyStatsRow.date == report.date, DailyStatsRow.mode == report.mode
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = DailyStatsRow(date=report.date, mode=report.mode)
                s.add(row)
            row.start_equity = report.start_equity
            row.end_equity = report.end_equity
            row.daily_pnl = report.daily_pnl
            row.trades = report.trades
            row.wins = report.wins
            row.losses = report.losses
            row.fees = report.fees
            row.best_trade = report.best_trade
            row.worst_trade = report.worst_trade
            row.max_dd = report.max_drawdown_pct
            row.errors = report.critical_errors
            row.report = (
                {k: (str(v) if isinstance(v, Decimal) else v) for k, v in report.__dict__.items()}
                if hasattr(report, "__dict__")
                else {}
            )

    await repo._write(_do)
    path = reports_dir / f"{report.date}.md"

    def _write_file() -> None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(report.to_markdown(), encoding="utf-8")

    await asyncio.to_thread(_write_file)
    return path
