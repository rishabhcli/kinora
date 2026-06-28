"""Invoice assembly + discount/tax/total math + invoice numbering (pure).

An invoice is built from **lines** — a flat plan fee, metered overage charges,
mid-period proration credits/charges, one-off adjustments — then a coupon
discount is applied to the pre-tax subtotal, tax is computed over the discounted
base, and the total is the sum. All amounts are integer minor units; the line
amounts always reconcile exactly with the header totals (no floating drift).

The math here is **pure**: it builds a :class:`DraftInvoice` value object from
line inputs. Persisting it (header + line rows) and finalizing it (assigning a
sequential human number) is the repository/service layer's job; the
:class:`InvoiceNumberFormatter` here just formats a number a sequence hands it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.billing.coupons import Coupon, apply_coupon
from app.billing.enums import TaxBehavior
from app.billing.money import DEFAULT_CURRENCY, Money, sum_money
from app.billing.tax import TaxRate, TaxResult, compute_tax


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One charge or credit on an invoice.

    ``amount`` is the *extended* amount for the line (``unit_amount * quantity``
    already applied for per-unit/metered lines; the assembler computes it). A
    negative amount is a credit (e.g. a proration credit).
    """

    description: str
    amount: Money
    quantity: float = 1.0
    unit_amount: Money | None = None
    proration: bool = False
    price_id: str | None = None
    meter: str | None = None

    @property
    def is_credit(self) -> bool:
        return self.amount.is_negative


@dataclass(frozen=True, slots=True)
class DraftInvoice:
    """A fully-computed (but not yet persisted) invoice.

    Totals reconcile: ``subtotal`` is the sum of line amounts; ``discount`` is the
    coupon amount (non-negative, taken off); ``taxed_base`` is
    ``subtotal − discount``; ``tax`` is computed over it; ``total`` is
    ``taxed_base + tax`` (exclusive) or ``taxed_base`` (inclusive).
    """

    currency: str
    lines: tuple[InvoiceLine, ...]
    subtotal: Money
    discount: Money
    taxed_base: Money
    tax: Money
    total: Money
    tax_result: TaxResult | None = None
    coupon_code: str | None = None

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def reconciles(self) -> bool:
        """Self-check that the header totals match the lines (used in tests)."""
        line_sum = sum_money([line.amount for line in self.lines], self.currency)
        if line_sum != self.subtotal:
            return False
        if (self.subtotal - self.discount) != self.taxed_base:
            return False
        expected_total = (
            self.taxed_base + self.tax
            if (self.tax_result is None or self.tax_result.behavior is TaxBehavior.EXCLUSIVE)
            else self.taxed_base
        )
        return expected_total == self.total


def line_for_charge(
    description: str,
    *,
    unit_amount: Money,
    quantity: int,
    price_id: str | None = None,
    meter: str | None = None,
    proration: bool = False,
) -> InvoiceLine:
    """Build a per-unit/metered line with the extended amount computed."""
    return InvoiceLine(
        description=description,
        amount=unit_amount * quantity,
        quantity=float(quantity),
        unit_amount=unit_amount,
        price_id=price_id,
        meter=meter,
        proration=proration,
    )


def assemble_invoice(
    lines: list[InvoiceLine],
    *,
    currency: str = DEFAULT_CURRENCY,
    coupon: Coupon | None = None,
    coupon_period_index: int = 0,
    tax_rates: list[TaxRate] | None = None,
    tax_behavior: TaxBehavior = TaxBehavior.EXCLUSIVE,
    at: datetime | None = None,
) -> DraftInvoice:
    """Assemble lines into a computed :class:`DraftInvoice`.

    Order of operations (the standard invoicing convention):

    1. subtotal = Σ line amounts
    2. discount = coupon applied to the *positive* subtotal (clamped ≥ 0)
    3. taxed_base = subtotal − discount
    4. tax = compute_tax(taxed_base, rates, behavior)
    5. total = taxed_base + tax (exclusive) or taxed_base (inclusive)

    A coupon never discounts a credit-only (≤0) subtotal. Tax is not applied to a
    negative base (a net-credit invoice has zero tax).
    """
    for line in lines:
        if line.amount.currency != currency:
            raise ValueError(
                f"line {line.description!r} is {line.amount.currency}, invoice is {currency}"
            )

    subtotal = sum_money([line.amount for line in lines], currency)

    # Coupon only bites on a positive subtotal.
    discount = Money.zero(currency)
    coupon_code: str | None = None
    if coupon is not None and subtotal.is_positive:
        when = at or datetime.now(tz=UTC)
        discount, _ = apply_coupon(subtotal, coupon, at=when, period_index=coupon_period_index)
        if discount.is_positive:
            coupon_code = coupon.code

    taxed_base = subtotal - discount

    tax_rates = tax_rates or []
    if taxed_base.is_positive and tax_rates:
        tax_result = compute_tax(taxed_base, tax_rates, behavior=tax_behavior)
        tax = tax_result.total_tax
        total = tax_result.total_with_tax if tax_behavior is TaxBehavior.EXCLUSIVE else taxed_base
    else:
        tax_result = None
        tax = Money.zero(currency)
        total = taxed_base

    return DraftInvoice(
        currency=currency,
        lines=tuple(lines),
        subtotal=subtotal,
        discount=discount,
        taxed_base=taxed_base,
        tax=tax,
        total=total,
        tax_result=tax_result,
        coupon_code=coupon_code,
    )


@dataclass
class InvoiceNumberFormatter:
    """Format a sequential invoice number into a human-readable identifier.

    The repository owns the monotonic sequence (a DB sequence / max+1); this just
    renders it as ``{prefix}-{year}-{seq:06d}`` (e.g. ``KIN-2026-000042``).
    """

    prefix: str = "KIN"

    def format(self, sequence: int, *, year: int | None = None) -> str:
        if sequence < 1:
            raise ValueError("invoice sequence must be >= 1")
        yr = year if year is not None else datetime.now(tz=UTC).year
        return f"{self.prefix}-{yr}-{sequence:06d}"


@dataclass
class InvoicePreview:
    """A lightweight projection of a draft for the 'next invoice' UI."""

    currency: str
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    lines: list[dict[str, object]] = field(default_factory=list)


def preview(draft: DraftInvoice) -> InvoicePreview:
    """Project a :class:`DraftInvoice` into a JSON-friendly preview."""
    return InvoicePreview(
        currency=draft.currency,
        subtotal_minor=draft.subtotal.amount_minor,
        discount_minor=draft.discount.amount_minor,
        tax_minor=draft.tax.amount_minor,
        total_minor=draft.total.amount_minor,
        lines=[
            {
                "description": line.description,
                "amount_minor": line.amount.amount_minor,
                "quantity": line.quantity,
                "proration": line.proration,
                "meter": line.meter,
            }
            for line in draft.lines
        ],
    )


__all__ = [
    "DraftInvoice",
    "InvoiceLine",
    "InvoiceNumberFormatter",
    "InvoicePreview",
    "assemble_invoice",
    "line_for_charge",
    "preview",
]
