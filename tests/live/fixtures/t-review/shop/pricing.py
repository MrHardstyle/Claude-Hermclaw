"""Preisberechnung für Bestellungen."""

from decimal import ROUND_HALF_UP, Decimal

COUPONS = {"SOMMER": Decimal("3.00")}


def discount_rate(quantity):
    if quantity > 50:
        return Decimal("0.10")
    if quantity >= 10:
        return Decimal("0.05")
    return Decimal("0")


def order_total(unit_price, quantity, coupon=None):
    if quantity <= 0:
        return Decimal("0.00")
    price = Decimal(str(unit_price)) * quantity
    price -= price * discount_rate(quantity)
    if coupon in COUPONS:
        price -= COUPONS[coupon]
    price = max(price, Decimal("0"))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
