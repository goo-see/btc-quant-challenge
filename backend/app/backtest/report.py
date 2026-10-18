from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.backtest.engine import BacktestResult


def format_metrics(r: BacktestResult) -> str:
    m = r.metrics
    lines = [
        f"Period            {r.start:%Y-%m-%d} -> {r.end:%Y-%m-%d}  ({m.bars} bars)",
        f"Starting Equity   ₩{m.starting_equity:,.0f}",
        f"Ending Equity     ₩{m.ending_equity:,.0f}",
        f"Net Profit        ₩{m.net_profit:,.0f}   Net Return {m.net_return_pct:.2f}%   (Buy&Hold {m.buy_hold_return_pct:.2f}%)",
        f"Trades            {m.trade_count}  W {m.win_count} / L {m.loss_count}  Win Rate {m.win_rate * 100:.1f}%",
        f"Avg Win / Loss    ₩{m.average_win:,.0f} / ₩{m.average_loss:,.0f}   Payoff {m.payoff_ratio:.2f}",
        f"Profit Factor     {m.profit_factor:.2f}   Expectancy ₩{m.expectancy:,.0f}/trade",
        f"Max Drawdown      {m.max_drawdown_pct:.2f}%   Max Consec Losses {m.max_consecutive_losses}",
        f"Sharpe (bar-ann.) {m.sharpe_ratio:.2f}   Exposure {m.exposure_pct:.1f}%",
        f"Fees Paid         ₩{m.fees_paid:,.0f}",
        f"Signals {r.signals}   Rejected BUYs {r.rejected_buys}",
    ]
    return "\n".join(lines)


def save_result(r: BacktestResult, out_dir: Path, name: str) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    eq_path = out_dir / f"{name}_equity.csv"
    with eq_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "equity"])
        for ts, eq in r.equity_curve:
            w.writerow([ts.isoformat(), str(eq)])
    tr_path = out_dir / f"{name}_trades.csv"
    with tr_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "quantity",
                "net_pnl",
                "fees",
                "return_pct",
                "holding_minutes",
                "entry_reason",
                "exit_reason",
            ]
        )
        for t in r.trades:
            w.writerow(
                [
                    t.entry_time,
                    t.exit_time,
                    t.entry_price,
                    t.exit_price,
                    t.quantity,
                    t.net_pnl,
                    t.fees,
                    t.return_pct,
                    t.holding_minutes,
                    t.entry_reason,
                    t.exit_reason,
                ]
            )
    summary: dict[str, Any] = {
        "start": r.start.isoformat(),
        "end": r.end.isoformat(),
        "params": r.params,
        "metrics": r.metrics.to_dict(),
        "signals": r.signals,
        "rejected_buys": r.rejected_buys,
    }
    js_path = out_dir / f"{name}_summary.json"
    js_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"equity": str(eq_path), "trades": str(tr_path), "summary": str(js_path)}
