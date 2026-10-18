from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.types import Fill, Side
from app.monitoring.slippage import measure

TS = datetime(2026, 1, 1, tzinfo=UTC)


def _fill(price: str, vol: str) -> Fill:
    p, v = Decimal(price), Decimal(vol)
    return Fill("c", p, v, p * v, Decimal(0), TS)


def test_buy_slippage_and_stop_distances() -> None:
    m = measure(
        side=Side.BID,
        signal_price=Decimal(100_000_000),
        fills=[_fill("100030000", "0.0002"), _fill("100050000", "0.0001")],
        initial_stop=Decimal(96_511_000),
    )
    assert m.actual_fill_price == Decimal("100036666.6666666666666666667")
    assert m.slippage_bps == Decimal("3.67")  # paid ~3.7 bps above the signal close
    assert m.stop_distance_from_signal == Decimal("0.034890")
    assert m.stop_distance_from_fill == Decimal("0.035244")
    assert m.fill_volume == Decimal("0.0003") and m.fill_count == 2
    assert m.to_dict()["slippage_bps"] == "3.67"


def test_sell_slippage_sign_is_adverse_positive() -> None:
    m = measure(
        side=Side.ASK,
        signal_price=Decimal(100_000_000),
        fills=[_fill("99970000", "0.0003")],
        initial_stop=None,
    )
    assert m.slippage_bps == Decimal("3.00") and m.stop_distance_from_fill is None
