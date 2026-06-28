"""Tests for invoice assembly, discount/tax/total math, and numbering."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.billing.coupons import Coupon
from app.billing.enums import DiscountType, TaxBehavior
from app.billing.invoicing import (
    InvoiceLine,
    InvoiceNumberFormatter,
    assemble_invoice,
    line_for_charge,
    preview,
)
from app.billing.money import Money
from app.billing.tax import TaxRate

NOW = datetime(2026, 6, 28, tzinfo=UTC)


def test_assemble_basic_subtotal() -> None:
    lines = [
        InvoiceLine("Pro plan", Money(2900)),
        InvoiceLine("Render overage", Money(500)),
    ]
    inv = assemble_invoice(lines)
    assert inv.subtotal.amount_minor == 3400
    assert inv.discount.is_zero
    assert inv.tax.is_zero
    assert inv.total.amount_minor == 3400
    assert inv.reconciles()


def test_line_for_charge_extends_amount() -> None:
    line = line_for_charge("Render overage", unit_amount=Money(2), quantity=200, meter="render")
    assert line.amount.amount_minor == 400
    assert line.quantity == 200.0


def test_assemble_with_proration_credit() -> None:
    lines = [
        InvoiceLine("Pro (remaining)", Money(1450), proration=True),
        InvoiceLine("Starter credit (unused)", Money(-450), proration=True),
    ]
    inv = assemble_invoice(lines)
    assert inv.subtotal.amount_minor == 1000
    assert inv.total.amount_minor == 1000
    assert inv.reconciles()


def test_assemble_with_percent_coupon() -> None:
    lines = [InvoiceLine("Pro plan", Money(2900))]
    coupon = Coupon("SAVE20", DiscountType.PERCENT, percent_off=Decimal("20"))
    inv = assemble_invoice(lines, coupon=coupon, at=NOW)
    assert inv.discount.amount_minor == 580  # 20% of 2900
    assert inv.taxed_base.amount_minor == 2320
    assert inv.total.amount_minor == 2320
    assert inv.coupon_code == "SAVE20"
    assert inv.reconciles()


def test_assemble_with_tax_exclusive() -> None:
    lines = [InvoiceLine("Pro plan", Money(10000))]
    inv = assemble_invoice(
        lines, tax_rates=[TaxRate("VAT", Decimal("0.20"))], tax_behavior=TaxBehavior.EXCLUSIVE
    )
    assert inv.taxed_base.amount_minor == 10000
    assert inv.tax.amount_minor == 2000
    assert inv.total.amount_minor == 12000
    assert inv.reconciles()


def test_assemble_with_tax_inclusive() -> None:
    lines = [InvoiceLine("Pro plan", Money(12000))]
    inv = assemble_invoice(
        lines, tax_rates=[TaxRate("VAT", Decimal("0.20"))], tax_behavior=TaxBehavior.INCLUSIVE
    )
    # Inclusive: total stays 12000; tax is the 2000 backed out of it.
    assert inv.total.amount_minor == 12000
    assert inv.tax.amount_minor == 2000
    assert inv.reconciles()


def test_coupon_then_tax_order() -> None:
    # $100, 20% coupon -> $80 base, then 10% tax -> $8 -> total $88.
    lines = [InvoiceLine("Plan", Money(10000))]
    coupon = Coupon("X", DiscountType.PERCENT, percent_off=Decimal("20"))
    inv = assemble_invoice(lines, coupon=coupon, tax_rates=[TaxRate("t", Decimal("0.10"))], at=NOW)
    assert inv.discount.amount_minor == 2000
    assert inv.taxed_base.amount_minor == 8000
    assert inv.tax.amount_minor == 800
    assert inv.total.amount_minor == 8800
    assert inv.reconciles()


def test_coupon_skipped_on_credit_invoice() -> None:
    # Net-credit invoice (downgrade): no coupon discount, no tax.
    lines = [InvoiceLine("Credit", Money(-1000))]
    coupon = Coupon("X", DiscountType.PERCENT, percent_off=Decimal("50"))
    inv = assemble_invoice(lines, coupon=coupon, tax_rates=[TaxRate("t", Decimal("0.10"))], at=NOW)
    assert inv.discount.is_zero
    assert inv.tax.is_zero
    assert inv.total.amount_minor == -1000
    assert inv.coupon_code is None


def test_currency_mismatch_line_rejected() -> None:
    lines = [InvoiceLine("Plan", Money(100, "EUR"))]
    with pytest.raises(ValueError):
        assemble_invoice(lines, currency="USD")


def test_invoice_number_formatter() -> None:
    fmt = InvoiceNumberFormatter()
    assert fmt.format(42, year=2026) == "KIN-2026-000042"
    assert fmt.format(1, year=2026) == "KIN-2026-000001"
    with pytest.raises(ValueError):
        fmt.format(0, year=2026)


def test_invoice_number_custom_prefix() -> None:
    fmt = InvoiceNumberFormatter(prefix="INV")
    assert fmt.format(7, year=2025) == "INV-2025-000007"


def test_preview_projection() -> None:
    lines = [InvoiceLine("Pro plan", Money(2900)), InvoiceLine("Overage", Money(500))]
    inv = assemble_invoice(lines, tax_rates=[TaxRate("t", Decimal("0.10"))])
    p = preview(inv)
    assert p.subtotal_minor == 3400
    assert p.total_minor == 3740  # +10% tax
    assert len(p.lines) == 2
    assert p.lines[0]["description"] == "Pro plan"
