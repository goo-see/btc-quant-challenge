"""Performance metrics. Fees and slippage are already inside every trade's net_pnl."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class TradeRecord:
    entry_time: str
    exit_time: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    fees: Decimal
    return_pct: Decimal
    holding_minutes: int
    entry_reason: str
    exit_reason: str


@dataclass(slots=True)
class Metrics:
    starting_equity: Decimal
    ending_equity: Decimal
    net_profit: Decimal
    net_return_pct: Decimal
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    average_win: Decimal
    average_loss: Decimal
    payoff_ratio: float
    profit_factor: float
    expectancy: Decimal
    max_drawdown_pct: Decimal
    max_consecutive_losses: int
    sharpe_ratio: float
    fees_paid: Decimal
    buy_hold_return_pct: Decimal
    exposure_pct: float
    bars: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def compute_metrics(
    *,
    starting_equity: Decimal,
    equity_curve: list[tuple[Any, Decimal]],
    trades: list[TradeRecord],
    fees_paid: Decimal,
    first_price: Decimal,
    last_price: Decimal,
    bars_per_year: float,
    bars_in_position: int,
) -> Metrics:
    ending = equity_curve[-1][1] if equity_curve else starting_equity
    net_profit = ending - starting_equity
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum((t.net_pnl for t in wins), Decimal(0))
    gross_loss = -sum((t.net_pnl for t in losses), Decimal(0))
    avg_win = gross_win / len(wins) if wins else Decimal(0)
    avg_loss = gross_loss / len(losses) if losses else Decimal(0)
    pf = float(gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    payoff = float(avg_win / avg_loss) if avg_loss > 0 else (math.inf if avg_win > 0 else 0.0)
    expectancy = (gross_win - gross_loss) / len(trades) if trades else Decimal(0)

    max_consec = cur = 0
    for t in trades:
        cur = cur + 1 if t.net_pnl <= 0 else 0
        max_consec = max(max_consec, cur)

    hwm = starting_equity
    mdd = Decimal(0)
    for _, eq in equity_curve:
        hwm = max(hwm, eq)
        if hwm > 0:
            mdd = max(mdd, (hwm - eq) / hwm)

    rets: list[float] = []
    for i in range(1, len(equity_curve)):
        prev, cur_eq = float(equity_curve[i - 1][1]), float(equity_curve[i][1])
        rets.append(cur_eq / prev - 1.0 if prev > 0 else 0.0)
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd) * math.sqrt(bars_per_year) if sd > 0 else 0.0

    return Metrics(
        starting_equity=starting_equity,
        ending_equity=ending,
        net_profit=net_profit,
        net_return_pct=(net_profit / starting_equity * 100) if starting_equity else Decimal(0),
        trade_count=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=(len(wins) / len(trades)) if trades else 0.0,
        average_win=avg_win,
        average_loss=avg_loss,
        payoff_ratio=payoff,
        profit_factor=pf,
        expectancy=expectancy,
        max_drawdown_pct=mdd * 100,
        max_consecutive_losses=max_consec,
        sharpe_ratio=sharpe,
        fees_paid=fees_paid,
        buy_hold_return_pct=((last_price / first_price) - 1) * 100 if first_price else Decimal(0),
        exposure_pct=(bars_in_position / len(equity_curve) * 100) if equity_curve else 0.0,
        bars=len(equity_curve),
    )
