"""Build a period invoice from a plan + recorded usage (the recurring run).

This is the bridge between the catalog/metering math and the invoice assembler:
given a plan, the period's aggregated usage, and an optional mid-period proration,
it produces the :class:`app.billing.invoicing.InvoiceLine` list (flat fee +
metered overage + proration lines), then assembles the draft invoice with coupon
+ tax applied.

Metered overage is computed by the catalog's tiered/graduated pricing
(:func:`app.billing.catalog.compute_price_charge`), where the price's
``included_units`` already model the plan's free allowance — so the billable
quantity passed to the price is the *raw* aggregated usage and the price subtracts
its inclusion. This keeps the §11-style "first N render-seconds are included,
overage is metered" exactly in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.billing.catalog import Plan, Price, compute_price_charge
from app.billing.coupons import Coupon
from app.billing.enums import BillingInterval, PriceType, TaxBehavior
from app.billing.invoicing import DraftInvoice, InvoiceLine, assemble_invoice
from app.billing.metering import UsageSummary
from app.billing.money import DEFAULT_CURRENCY, Money
from app.billing.proration import ProrationResult
from app.billing.tax import TaxRate


@dataclass(frozen=True, slots=True)
class PeriodChargeContext:
    """Inputs needed to bill one period of a subscription."""

    plan: Plan
    interval: BillingInterval
    usage: UsageSummary
    currency: str = DEFAULT_CURRENCY
    proration: ProrationResult | None = None
    coupon: Coupon | None = None
    coupon_period_index: int = 0
    tax_rates: tuple[TaxRate, ...] = ()
    tax_behavior: TaxBehavior = TaxBehavior.EXCLUSIVE
    at: datetime | None = None


def _flat_lines(plan: Plan, interval: BillingInterval, currency: str) -> list[InvoiceLine]:
    """The plan's flat recurring fee line(s) for the matching interval."""
    lines: list[InvoiceLine] = []
    for price in plan.prices:
        if price.type is PriceType.FLAT and price.interval is interval:
            assert price.flat_amount is not None
            if price.flat_amount.currency != currency:
                continue
            if price.flat_amount.is_zero:
                continue  # free plan: no line
            lines.append(
                InvoiceLine(
                    description=f"{plan.name} ({interval.value})",
                    amount=price.flat_amount,
                    price_id=price.id,
                )
            )
    return lines


def _metered_line(price: Price, usage: UsageSummary, currency: str) -> InvoiceLine | None:
    """An overage line for a metered ``price`` given the period's usage (or None)."""
    if price.meter is None:
        return None
    used = usage.quantity(price.meter)
    quantity = int(round(used))
    charge = compute_price_charge(price, quantity)
    billable = max(0, quantity - price.included_units)
    if charge.is_zero:
        return None
    return InvoiceLine(
        description=f"{price.meter.value} overage ({billable} units)",
        amount=charge,
        quantity=float(billable),
        unit_amount=price.unit_amount,
        price_id=price.id,
        meter=price.meter.value,
    )


def _proration_lines(proration: ProrationResult) -> list[InvoiceLine]:
    """Charge + credit lines for a mid-period plan change."""
    lines: list[InvoiceLine] = []
    if not proration.charge.is_zero:
        lines.append(
            InvoiceLine(
                description="Proration: remaining period on new plan",
                amount=proration.charge,
                proration=True,
            )
        )
    if not proration.credit.is_zero:
        lines.append(
            InvoiceLine(
                description="Proration: unused period on previous plan",
                amount=proration.credit,
                proration=True,
            )
        )
    return lines


def build_period_lines(ctx: PeriodChargeContext) -> list[InvoiceLine]:
    """Assemble the full invoice-line list for one billing period."""
    lines: list[InvoiceLine] = []
    if ctx.proration is not None:
        lines.extend(_proration_lines(ctx.proration))
    lines.extend(_flat_lines(ctx.plan, ctx.interval, ctx.currency))
    for price in ctx.plan.prices:
        if price.type is PriceType.METERED:
            metered = _metered_line(price, ctx.usage, ctx.currency)
            if metered is not None:
                lines.append(metered)
    return lines


def build_period_invoice(ctx: PeriodChargeContext) -> DraftInvoice:
    """Build + assemble the period's :class:`DraftInvoice` (lines, coupon, tax)."""
    lines = build_period_lines(ctx)
    if not lines:
        # A zero-fee period (e.g. Free plan, no overage): emit an empty $0 invoice.
        lines = [InvoiceLine(description="No charges this period", amount=Money.zero(ctx.currency))]
    return assemble_invoice(
        lines,
        currency=ctx.currency,
        coupon=ctx.coupon,
        coupon_period_index=ctx.coupon_period_index,
        tax_rates=list(ctx.tax_rates),
        tax_behavior=ctx.tax_behavior,
        at=ctx.at,
    )


__all__ = [
    "PeriodChargeContext",
    "build_period_invoice",
    "build_period_lines",
]
