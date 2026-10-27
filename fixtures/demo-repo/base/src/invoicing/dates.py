"""Billing periods and proration."""

import calendar
from datetime import date, timedelta
from decimal import Decimal

from invoicing.money import to_money


def billing_days(start: date, end: date) -> list[date]:
    """Every day of a billing period, including both ``start`` and ``end``."""
    if end < start:
        raise ValueError(f"period ends ({end}) before it starts ({start})")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def prorate(monthly_fee: Decimal, start: date, end: date) -> Decimal:
    """Charge for part of a month: the fee times the share of the month's days used."""
    if (start.year, start.month) != (end.year, end.month):
        raise ValueError("a prorated period must fall within a single month")
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    used = len(billing_days(start, end))
    return to_money(monthly_fee * used / days_in_month)
