"""Portfolio manager: accounting state + persistence + daily tracking (KST boundary).

The full state is persisted as JSON in bot_state['portfolio'] after every change so a
restarted process resumes exactly; trades/fills/snapshots tables hold the history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.types import OrderRecord, Side, TradingMode
from app.database.repository import Repository
from app.portfolio.accounting import ZERO, PortfolioState, q_krw
from app.strategy.base import PositionView

log = get_logger("portfolio")


@dataclass(slots=True)
class DailyTracker:
    day_key: str
    start_equity: Decimal
    trades: int = 0
    wins: int = 0
    losses: int = 0
    fees: Decimal = ZERO
    realized: Decimal = ZERO


class PortfolioManager:
    STATE_KEY = "portfolio"

    def __init__(
        self,
        repo: Repository | None,
        mode: TradingMode,
        initial_equity: Decimal,
        *,
        report_tz: str = "Asia/Seoul",
        strategy_name: str = "",
    ) -> None:
        self._repo = repo
        self.mode = mode
        self.state = PortfolioState(initial_equity=initial_equity, krw=initial_equity)
        self._tz = ZoneInfo(report_tz)
        self.strategy_name = strategy_name
        self.last_price: Decimal | None = None
        self.last_price_at: datetime | None = None
        self.open_trade_id: int | None = None
        self.stop_price: Decimal | None = None
        self.entry_reason: str = ""
        self.consecutive_losses = 0
        self.last_loss_at: datetime | None = None
        now = datetime.now(UTC)
        self.daily = DailyTracker(self._day_key(now), initial_equity)

    # ------------------------------------------------------------ helpers
    def _day_key(self, ts: datetime) -> str:
        return ts.astimezone(self._tz).strftime("%Y-%m-%d")

    @property
    def price(self) -> Decimal:
        if self.last_price is None:
            raise RuntimeError("no market price yet")
        return self.last_price

    def equity(self) -> Decimal:
        return self.state.equity(self.price)

    def daily_pnl(self) -> Decimal:
        return q_krw(self.equity() - self.daily.start_equity)

    def drawdown(self) -> Decimal:
        return self.state.drawdown(self.price)

    def position_view(self) -> PositionView | None:
        if not self.state.has_position or self.state.avg_entry is None:
            return None
        return PositionView(
            quantity=self.state.btc,
            avg_entry=self.state.avg_entry,
            entry_time=self.state.entry_time or datetime.now(UTC),
            stop_price=self.stop_price,
            highest_since_entry=self.state.highest_since_entry or self.price,
        )

    def on_price(self, price: Decimal, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(UTC)
        self.last_price, self.last_price_at = price, ts
        self.state.mark(price)
        key = self._day_key(ts)
        if key != self.daily.day_key:
            log.info(
                "daily_rollover", from_day=self.daily.day_key, to_day=key, equity=str(self.equity())
            )
            self.daily = DailyTracker(key, self.equity())

    def update_stop(self, stop: Decimal | None) -> None:
        """Ratchet only: a trailing stop never moves down."""
        if stop is None:
            return
        if self.stop_price is None or stop > self.stop_price:
            self.stop_price = stop

    # -------------------------------------------------------------- fills
    async def apply_order(
        self, rec: OrderRecord, *, reason: str, stop: Decimal | None = None
    ) -> Decimal | None:
        """Apply a (terminal) order's fills. Returns realized PnL for sells."""
        if not rec.fills:
            return None
        realized_total: Decimal | None = None
        for f in rec.fills:
            if rec.side is Side.BID:
                self.state.apply_buy(f)
            else:
                realized = self.state.apply_sell(f)
                realized_total = (realized_total or ZERO) + realized
        if rec.side is Side.BID:
            self.entry_reason = reason
            self.stop_price = stop
            if self._repo is not None and self.open_trade_id is None:
                self.open_trade_id = await self._repo.open_trade(
                    mode=self.mode,
                    entry_order_id=rec.db_id,
                    entry_time=rec.fills[0].timestamp,
                    entry_price=self.state.avg_entry or rec.fills[0].price,
                    quantity=self.state.btc,
                    fees=sum((f.fee for f in rec.fills), ZERO),
                    strategy=self.strategy_name,
                    entry_reason=reason,
                )
        else:
            assert realized_total is not None
            self._on_realized(realized_total, rec.fills[-1].timestamp)
            if not self.state.has_position:
                if self._repo is not None and self.open_trade_id is not None:
                    entry_price = rec.raw.get("entry_price")
                    qty = sum((f.volume for f in rec.fills), ZERO)
                    exit_price = (
                        sum((f.funds for f in rec.fills), ZERO) / qty
                        if qty
                        else rec.fills[-1].price
                    )
                    fees_total = sum((f.fee for f in rec.fills), ZERO) + Decimal(
                        str(rec.raw.get("entry_fees", "0"))
                    )
                    gross = realized_total + fees_total
                    basis = Decimal(str(entry_price)) * qty if entry_price else ZERO
                    await self._repo.close_trade(
                        self.open_trade_id,
                        exit_order_id=rec.db_id,
                        exit_time=rec.fills[-1].timestamp,
                        exit_price=exit_price,
                        gross_pnl=gross,
                        fees_total=fees_total,
                        net_pnl=realized_total,
                        return_pct=(realized_total / basis * 100) if basis else ZERO,
                        exit_reason=reason,
                    )
                self.open_trade_id = None
                self.stop_price = None
                self.entry_reason = ""
        await self.persist()
        return realized_total

    def _on_realized(self, pnl: Decimal, ts: datetime) -> None:
        self.daily.trades += 1
        self.daily.realized += pnl
        if pnl > 0:
            self.daily.wins += 1
            self.consecutive_losses = 0
        else:
            self.daily.losses += 1
            self.consecutive_losses += 1
            self.last_loss_at = ts

    # -------------------------------------------------------- persistence
    def to_json(self) -> dict[str, Any]:
        s = self.state
        return {
            "state": {
                "initial_equity": str(s.initial_equity),
                "krw": str(s.krw),
                "btc": str(s.btc),
                "avg_entry": str(s.avg_entry) if s.avg_entry is not None else None,
                "entry_fees": str(s.entry_fees),
                "entry_time": s.entry_time.isoformat() if s.entry_time else None,
                "highest_since_entry": str(s.highest_since_entry)
                if s.highest_since_entry is not None
                else None,
                "realized_pnl": str(s.realized_pnl),
                "fees_total": str(s.fees_total),
                "hwm": str(s.hwm),
                "max_drawdown": str(s.max_drawdown),
                "trade_count": s.trade_count,
            },
            "open_trade_id": self.open_trade_id,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "entry_reason": self.entry_reason,
            "consecutive_losses": self.consecutive_losses,
            "last_loss_at": self.last_loss_at.isoformat() if self.last_loss_at else None,
            "daily": {
                "day_key": self.daily.day_key,
                "start_equity": str(self.daily.start_equity),
                "trades": self.daily.trades,
                "wins": self.daily.wins,
                "losses": self.daily.losses,
                "fees": str(self.daily.fees),
                "realized": str(self.daily.realized),
            },
        }

    def restore(self, d: dict[str, Any]) -> None:
        st = d["state"]

        def dec(k: str) -> Decimal | None:
            return Decimal(st[k]) if st.get(k) is not None else None

        self.state = PortfolioState(
            initial_equity=Decimal(st["initial_equity"]),
            krw=Decimal(st["krw"]),
            btc=Decimal(st["btc"]),
            avg_entry=dec("avg_entry"),
            entry_fees=Decimal(st["entry_fees"]),
            entry_time=datetime.fromisoformat(st["entry_time"]) if st.get("entry_time") else None,
            highest_since_entry=dec("highest_since_entry"),
            realized_pnl=Decimal(st["realized_pnl"]),
            fees_total=Decimal(st["fees_total"]),
            hwm=Decimal(st["hwm"]),
            max_drawdown=Decimal(st["max_drawdown"]),
            trade_count=int(st["trade_count"]),
        )
        self.open_trade_id = d.get("open_trade_id")
        self.stop_price = Decimal(d["stop_price"]) if d.get("stop_price") else None
        self.entry_reason = d.get("entry_reason", "")
        self.consecutive_losses = int(d.get("consecutive_losses", 0))
        self.last_loss_at = (
            datetime.fromisoformat(d["last_loss_at"]) if d.get("last_loss_at") else None
        )
        dd = d["daily"]
        self.daily = DailyTracker(
            dd["day_key"],
            Decimal(dd["start_equity"]),
            int(dd["trades"]),
            int(dd["wins"]),
            int(dd["losses"]),
            Decimal(dd["fees"]),
            Decimal(dd["realized"]),
        )

    async def persist(self) -> None:
        if self._repo is not None:
            await self._repo.set_state(self.STATE_KEY, self.to_json())

    async def load(self) -> bool:
        if self._repo is None:
            return False
        d = await self._repo.get_state(self.STATE_KEY)
        if not d:
            return False
        self.restore(d)
        return True

    async def save_snapshot(self) -> None:
        if self._repo is None or self.last_price is None:
            return
        p = self.price
        s = self.state
        await self._repo.add_snapshot(
            self.mode,
            {
                "krw": s.krw,
                "btc_qty": s.btc,
                "btc_price": p,
                "btc_value": s.btc_value(p),
                "equity": s.equity(p),
                "avg_entry": s.avg_entry,
                "realized_pnl": s.realized_pnl,
                "unrealized_pnl": s.unrealized_pnl(p),
                "fees_total": s.fees_total,
                "hwm": s.hwm,
                "drawdown": s.drawdown(p),
            },
        )
