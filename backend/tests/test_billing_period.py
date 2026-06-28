"""Tests for building a period invoice from a plan + recorded usage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.billing.coupons import Coupon
from app.billing.default_catalog import FREE_PLAN, PRO_PLAN, STARTER_PLAN
from app.billing.enums import (
    BillingInterval,
    DiscountType,
    MeteredAggregation,
    UsageMeter,
)
from app.billing.metering import UsageQuantity, UsageSummary
from app.billing.money import Money
from app.billing.period_billing import (
    PeriodChargeContext,
    build_period_invoice,
    build_period_lines,
)
from app.billing.proration import ProrationResult
from app.billing.tax import TaxRate

NOW = datetime(2026, 6, 28, tzinfo=UTC)


def usage_of(meter: UsageMeter, qty: float) -> UsageSummary:
    s = UsageSummary(period_start=None, period_end=None)
    s.by_meter[meter] = UsageQuantity(meter, MeteredAggregation.SUM, qty, 1)
    return s


def test_starter_no_overage() -> None:
    # 250 render-seconds, all within Starter's 300 included -> flat fee only.
    ctx = PeriodChargeContext(
        plan=STARTER_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 250),
    )
    inv = build_period_invoice(ctx)
    assert inv.subtotal.amount_minor == 900  # $9 flat
    assert inv.line_count == 1
    assert inv.reconciles()


def test_starter_with_overage() -> None:
    # 500 render-seconds: 200 over @2¢ = 400; + $9 flat = $13.00.
    ctx = PeriodChargeContext(
        plan=STARTER_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 500),
    )
    inv = build_period_invoice(ctx)
    assert inv.subtotal.amount_minor == 900 + 400
    assert inv.line_count == 2
    assert inv.reconciles()


def test_pro_graduated_overage_line() -> None:
    # Pro: 1200 included; used 3200 -> 2000 over -> 1800@2¢ + 200@1¢ = 3800.
    ctx = PeriodChargeContext(
        plan=PRO_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 3200),
    )
    inv = build_period_invoice(ctx)
    # $29 flat + $38 overage = $67.
    assert inv.subtotal.amount_minor == 2900 + 3800
    assert inv.reconciles()


def test_free_plan_zero_invoice() -> None:
    ctx = PeriodChargeContext(
        plan=FREE_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.READING_MINUTES, 9999),
    )
    inv = build_period_invoice(ctx)
    assert inv.total.is_zero
    assert inv.line_count == 1  # the "No charges this period" placeholder


def test_proration_lines_included() -> None:
    proration = ProrationResult(credit=Money(-450), charge=Money(1450))
    ctx = PeriodChargeContext(
        plan=PRO_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 0),
        proration=proration,
    )
    lines = build_period_lines(ctx)
    descs = [line.description for line in lines]
    assert any("remaining period on new plan" in d for d in descs)
    assert any("unused period on previous plan" in d for d in descs)
    # plus the $29 flat fee.
    assert any("Pro" in d for d in descs)


def test_period_invoice_with_coupon_and_tax() -> None:
    coupon = Coupon("SAVE10", DiscountType.PERCENT, percent_off=Decimal("10"))
    ctx = PeriodChargeContext(
        plan=PRO_PLAN,
        interval=BillingInterval.MONTH,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 0),
        coupon=coupon,
        tax_rates=(TaxRate("VAT", Decimal("0.20")),),
        at=NOW,
    )
    inv = build_period_invoice(ctx)
    # $29 - 10% = $26.10 base; +20% tax = $5.22 -> total $31.32.
    assert inv.subtotal.amount_minor == 2900
    assert inv.discount.amount_minor == 290
    assert inv.taxed_base.amount_minor == 2610
    assert inv.tax.amount_minor == 522
    assert inv.total.amount_minor == 3132
    assert inv.reconciles()


def test_yearly_interval_picks_year_price() -> None:
    ctx = PeriodChargeContext(
        plan=PRO_PLAN,
        interval=BillingInterval.YEAR,
        usage=usage_of(UsageMeter.RENDER_SECONDS, 0),
    )
    inv = build_period_invoice(ctx)
    assert inv.subtotal.amount_minor == 29000  # $290 annual
