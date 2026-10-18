from __future__ import annotations

from decimal import Decimal

import pytest

from app.exchange.upbit.tick_size import is_valid_price, krw_tick, meets_min_total, round_price


@pytest.mark.parametrize(
    ("price", "tick"),
    [
        ("115313000", "1000"),
        ("2000000", "1000"),
        ("1999999", "1000"),
        ("1000000", "1000"),
        ("999999", "500"),
        ("500000", "500"),
        ("499999", "100"),
        ("100000", "100"),
        ("99999", "50"),
        ("50000", "50"),
        ("49999", "10"),
        ("10000", "10"),
        ("9999", "5"),
        ("5000", "5"),
        ("4999", "1"),
        ("1000", "1"),
        ("999", "1"),
        ("100", "1"),
        ("99", "0.1"),
        ("10", "0.1"),
        ("9", "0.01"),
        ("1", "0.01"),
        ("0.5", "0.001"),
    ],
)
def test_krw_tick_table(price: str, tick: str) -> None:
    assert krw_tick(Decimal(price)) == Decimal(tick)


def test_round_price_btc_range() -> None:
    assert round_price(Decimal("115313499"), "down") == Decimal("115313000")
    assert round_price(Decimal("115313499"), "up") == Decimal("115314000")
    assert is_valid_price(Decimal("115313000"))
    assert not is_valid_price(Decimal("115313500"))


def test_doc_example_15_won() -> None:
    for ok in ("14.7", "14.8", "15.0", "15.2"):
        assert is_valid_price(Decimal(ok))
    assert not is_valid_price(Decimal("15.01"))


def test_min_total() -> None:
    assert meets_min_total(Decimal("115000000"), Decimal("0.00005"))  # 5,750
    assert not meets_min_total(Decimal("115000000"), Decimal("0.00004"))  # 4,600
