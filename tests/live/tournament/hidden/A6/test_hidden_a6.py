"""A6: the three review items are fixed and every earlier README rule still holds."""

from decimal import Decimal

import pytest

from shop.pricing import order_total


def test_review_1_non_positive_quantity_raises():
    for q in (0, -1):
        with pytest.raises(ValueError):
            order_total("5", q)


def test_review_2_coupon_ignores_case_and_spaces():
    assert order_total("10", 1, " sommer ") == Decimal("7.00")
    assert order_total("10", 1, "Sommer") == Decimal("7.00")


def test_review_3_ten_percent_from_exactly_fifty():
    assert order_total("1", 50) == Decimal("45.00")
    assert order_total("1", 49) == Decimal("46.55")


def test_old_rules_still_hold():
    assert order_total("2.50", 4) == Decimal("10.00")
    assert order_total("10", 10) == Decimal("95.00")
    assert order_total("10", 10, "SOMMER") == Decimal("92.00")
    assert order_total("1", 1, "SOMMER") == Decimal("0.00")
    assert order_total("1", 1, "WINTER") == Decimal("1.00")
    assert order_total("0.125", 1) == Decimal("0.13")
    assert order_total("1", 9) == Decimal("9.00")
    assert order_total(10, 2) == Decimal("20.00")
    assert isinstance(order_total("1", 1), Decimal)
