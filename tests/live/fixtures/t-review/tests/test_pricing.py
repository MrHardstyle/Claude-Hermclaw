from decimal import Decimal

from shop.pricing import order_total


def test_plain_price():
    assert order_total("2.50", 4) == Decimal("10.00")


def test_five_percent_from_ten_pieces():
    assert order_total("10", 10) == Decimal("95.00")


def test_coupon_after_discount():
    assert order_total("10", 10, "SOMMER") == Decimal("92.00")


def test_total_never_negative():
    assert order_total("1", 1, "SOMMER") == Decimal("0.00")
