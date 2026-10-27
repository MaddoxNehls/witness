from decimal import Decimal

from invoicing.invoice import Invoice, LineItem


def test_invoice_total_with_tax() -> None:
    invoice = Invoice(
        customer="ACME",
        lines=[
            LineItem("Seat licence", Decimal("40.00"), quantity=3),
            LineItem("Onboarding", Decimal("100.00"), discount_percent=Decimal(10)),
        ],
        tax_rate=Decimal("0.20"),
    )
    assert invoice.subtotal() == Decimal("210.00")
    assert invoice.total() == Decimal("252.00")
