from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.types import Fill
from app.portfolio.accounting import PortfolioState

TS = datetime(2026, 1, 1, tzinfo=UTC)


def _opt(x: Decimal | None) -> Decimal | None:
    return x


def _fill(price: str, vol: str, fee_rate: str = "0.0005") -> Fill:
    p, v = Decimal(price), Decimal(vol)
    funds = p * v
    return Fill("x", p, v, funds, (funds * Decimal(fee_rate)).quantize(Decimal("0.01")), TS)


def test_buy_then_sell_at_profit_accounts_fees_both_sides() -> None:
    ps = PortfolioState(initial_equity=Decimal(100000), krw=Decimal(100000))
    buy = _fill("100000000", "0.0003")  # 30,000 KRW, fee 15
    ps.apply_buy(buy)
    assert ps.krw == Decimal("69985.00")
    assert ps.btc == Decimal("0.0003")
    assert ps.avg_entry == Decimal(100000000)
    assert ps.equity(Decimal(100000000)) == Decimal("99985.00")  # only the fee is lost
    assert ps.unrealized_pnl(Decimal(100000000)) == Decimal("-15.00")

    sell = _fill("110000000", "0.0003")  # 33,000 KRW, fee 16.5
    net = ps.apply_sell(sell)
    assert net == Decimal("3000") - Decimal("16.50") - Decimal("15.00")
    assert ps.realized_pnl == net
    assert ps.btc == 0 and not ps.has_position and ps.trade_count == 1
    assert _opt(ps.avg_entry) is None
    assert ps.krw == Decimal("69985.00") + Decimal("33000") - Decimal("16.50")
    assert ps.fees_total == Decimal("31.50")


def test_average_entry_on_two_buys() -> None:
    ps = PortfolioState(initial_equity=Decimal(100000), krw=Decimal(100000))
    ps.apply_buy(_fill("100000000", "0.0001"))
    ps.apply_buy(_fill("120000000", "0.0001"))
    assert ps.avg_entry == Decimal(110000000)


def test_drawdown_and_hwm_tracking() -> None:
    ps = PortfolioState(initial_equity=Decimal(100000), krw=Decimal(100000))
    ps.apply_buy(_fill("100000000", "0.0005", "0"))  # 50k in BTC
    ps.mark(Decimal(120000000))  # equity 110k -> HWM 110k
    assert ps.hwm == Decimal("110000.00")
    ps.mark(Decimal(100000000))  # equity 100k -> dd 9.09%
    assert ps.drawdown(Decimal(100000000)).quantize(Decimal("0.0001")) == Decimal("0.0909")
    assert ps.max_drawdown.quantize(Decimal("0.0001")) == Decimal("0.0909")
    ps.mark(Decimal(130000000))
    assert ps.max_drawdown.quantize(Decimal("0.0001")) == Decimal("0.0909")  # never shrinks


def test_insufficient_funds_raise() -> None:
    ps = PortfolioState(initial_equity=Decimal(1000), krw=Decimal(1000))
    with pytest.raises(ValueError, match="insufficient KRW"):
        ps.apply_buy(_fill("100000000", "0.0003"))
    with pytest.raises(ValueError, match="insufficient BTC"):
        ps.apply_sell(_fill("100000000", "0.0001"))
