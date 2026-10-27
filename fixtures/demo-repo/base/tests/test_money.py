from decimal import Decimal

import pytest
from invoicing.money import apply_discount, to_money


def test_to_money_rounds_half_up() -> None:
    assert to_money(Decimal("2.345")) == Decimal("2.35")


def test_apply_discount() -> None:
    assert apply_discount(Decimal("200.00"), Decimal(25)) == Decimal("150.00")


def test_discount_out_of_range() -> None:
    with pytest.raises(ValueError):
        apply_discount(Decimal("10.00"), Decimal(120))
