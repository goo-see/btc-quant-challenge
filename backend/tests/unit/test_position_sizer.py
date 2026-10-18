from __future__ import annotations

from decimal import Decimal

from app.risk.position_sizer import SizingParams, size_buy_krw

P = SizingParams(
    max_position_pct=Decimal("0.30"),
    max_order_krw=Decimal(30000),
    min_order_krw=Decimal(5000),
    fee_rate=Decimal("0.0005"),
    minimum_krw_balance=Decimal(1000),
)


def test_live_v1_caps_at_30pct_and_30000() -> None:
    amt, why = size_buy_krw(Decimal(100000), Decimal(100000), P)
    assert amt == Decimal(30000) and why == "OK"
    amt, _ = size_buy_krw(Decimal(200000), Decimal(200000), P)
    assert amt == Decimal(30000)  # max_order_krw binds


def test_size_never_grows_after_loss() -> None:
    before, _ = size_buy_krw(Decimal(100000), Decimal(100000), P)
    after, _ = size_buy_krw(Decimal(90000), Decimal(90000), P)  # equity fell 10%
    assert before is not None and after is not None and after <= before


def test_fee_room_and_min_balance_respected() -> None:
    amt, _ = size_buy_krw(Decimal(100000), Decimal(7000), P)
    assert amt is not None
    assert amt * (1 + P.fee_rate) <= Decimal(7000) - P.minimum_krw_balance


def test_below_min_order_rejected() -> None:
    amt, why = size_buy_krw(Decimal(100000), Decimal(5500), P)
    assert amt is None and why.startswith("MIN_ORDER")
