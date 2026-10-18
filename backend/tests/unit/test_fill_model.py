from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.execution.fill_model import ExitKind, FillModel, FillParams
from tests.conftest import make_candle

TS = datetime(2026, 1, 1, tzinfo=UTC)


def test_market_buy_applies_slippage_and_fee_on_top() -> None:
    fm = FillModel(FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(10)))
    f = fm.market_buy("c", Decimal(30000), Decimal(100000000), TS)
    assert f.price == Decimal("100100000.0000")  # +10bps
    assert f.volume == Decimal("0.00029970")
    assert f.fee == (f.funds * Decimal("0.0005")).quantize(Decimal("0.01"))
    assert f.funds + f.fee <= Decimal(30000) * (1 + Decimal("0.0005"))


def test_market_sell_slippage_negative() -> None:
    fm = FillModel(FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(10)))
    f = fm.market_sell("c", Decimal("0.0003"), Decimal(100000000), TS)
    assert f.price == Decimal("99900000.0000")
    assert f.funds == Decimal("29970.00")
    assert f.fee == Decimal("14.99")


def test_max_buy_krw_leaves_room_for_fee() -> None:
    fm = FillModel(FillParams(fee_rate=Decimal("0.0005")))
    amt = fm.max_buy_krw(Decimal(100000))
    assert amt * (1 + Decimal("0.0005")) <= Decimal(100000)


# ---- REQUIRED POLICY: stop-loss priority when stop and target hit in the same bar ----


def test_stop_and_target_same_bar_resolves_to_stop() -> None:
    bar = make_candle(0, open_=100, high=120, low=80, close=110)
    ex = FillModel.resolve_intrabar_exit(bar, stop_price=Decimal(90), target_price=Decimal(115))
    assert ex is not None
    assert ex.kind is ExitKind.STOP
    assert ex.price == Decimal(90)
    assert "STOP_PRIORITY" in ex.reason


def test_gap_down_through_stop_fills_at_open_not_stop() -> None:
    bar = make_candle(0, open_=85, high=88, low=80, close=86)
    ex = FillModel.resolve_intrabar_exit(bar, stop_price=Decimal(90), target_price=None)
    assert ex is not None and ex.kind is ExitKind.STOP
    assert ex.price == Decimal(85)  # worse than the stop, never better


def test_only_target_hit_fills_at_target_even_if_gapped_above() -> None:
    bar = make_candle(0, open_=120, high=125, low=118, close=121)
    ex = FillModel.resolve_intrabar_exit(bar, stop_price=Decimal(90), target_price=Decimal(115))
    assert ex is not None and ex.kind is ExitKind.TARGET and ex.price == Decimal(115)


def test_no_exit_when_neither_touched() -> None:
    bar = make_candle(0, open_=100, high=105, low=95, close=101)
    assert FillModel.resolve_intrabar_exit(bar, Decimal(90), Decimal(115)) is None
    assert FillModel.resolve_intrabar_exit(bar, None, None) is None


def test_zero_volume_buy_is_rejected_not_divided_by_zero() -> None:
    import pytest

    from app.execution.fill_model import ZeroVolumeError

    fm = FillModel(FillParams())
    with pytest.raises(ZeroVolumeError):
        fm.market_buy("c", Decimal(30000), Decimal("1e13"), TS)  # 30k KRW < 1e-8 BTC
