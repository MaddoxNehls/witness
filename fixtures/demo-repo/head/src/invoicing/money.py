"""Money arithmetic. Amounts are Decimal, rounded half-up to the cent."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value: Decimal | int | str) -> Decimal:
    """Round to the cent, half-up, as invoices are printed."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def apply_discount(amount: Decimal, percent: Decimal | float) -> Decimal:
    """Reduce ``amount`` by ``percent`` (0-100) and round to the cent.

    Accepts float percentages as sent by the pricing API, as well as Decimals.
    """
    if not 0 <= percent <= 100:
        raise ValueError(f"discount must be between 0 and 100, got {percent}")
    discounted = float(amount) * (1 - float(percent) / 100)
    return Decimal(str(round(discounted, 2)))
