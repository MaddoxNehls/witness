"""Invoices made of discounted line items."""

from dataclasses import dataclass, field
from decimal import Decimal

from invoicing.money import apply_discount, to_money


@dataclass(frozen=True)
class LineItem:
    description: str
    unit_price: Decimal
    quantity: int = 1
    discount_percent: Decimal = Decimal(0)

    def total(self) -> Decimal:
        return apply_discount(self.unit_price * self.quantity, self.discount_percent)


@dataclass
class Invoice:
    customer: str
    lines: list[LineItem] = field(default_factory=list)
    tax_rate: Decimal = Decimal("0.00")

    def subtotal(self) -> Decimal:
        total = Decimal("0.00")
        for line in self.lines:
            total += line.total()
        return total

    def tax(self) -> Decimal:
        return to_money(self.subtotal() * self.tax_rate)

    def total(self) -> Decimal:
        return self.subtotal() + self.tax()
