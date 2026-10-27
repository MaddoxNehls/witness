from datetime import date
from decimal import Decimal

import pytest
from invoicing.dates import prorate


def test_prorate_rejects_cross_month_period() -> None:
    with pytest.raises(ValueError):
        prorate(Decimal("30.00"), date(2026, 1, 20), date(2026, 2, 5))


def test_prorate_rejects_reversed_period() -> None:
    with pytest.raises(ValueError):
        prorate(Decimal("30.00"), date(2026, 4, 9), date(2026, 4, 1))
