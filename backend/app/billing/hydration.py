"""Bridge ORM rows <-> the pure catalog value objects.

The pricing/entitlements math operates on the immutable
:class:`app.billing.catalog.Plan` / :class:`Price` value objects; persistence
uses the ORM rows in :mod:`app.billing.models`. This module converts between the
two so the service can hydrate a DB plan into the pure objects, run the math, and
persist results — without the math ever importing SQLAlchemy.
"""

from __future__ import annotations

from app.billing.catalog import Feature, Plan, Price, PriceTier
from app.billing.coupons import Coupon
from app.billing.models import BillingCoupon, BillingPlan, BillingPrice
from app.billing.money import Money


def price_from_row(row: BillingPrice) -> Price:
    """Convert a :class:`BillingPrice` ORM row to a catalog :class:`Price`."""
    tiers: tuple[PriceTier, ...] = ()
    if row.tiers:
        tiers = tuple(
            PriceTier(
                up_to=t.get("up_to"),
                unit_amount=Money(int(t["unit_amount_minor"]), row.currency),
                flat_amount=(
                    Money(int(t["flat_amount_minor"]), row.currency)
                    if t.get("flat_amount_minor") is not None
                    else None
                ),
            )
            for t in row.tiers
        )
    return Price(
        id=row.id,
        plan_code="",  # filled by plan_from_row; not needed for charge math
        type=row.type,
        interval=row.interval,
        currency=row.currency,
        flat_amount=(
            Money(row.flat_amount_minor, row.currency)
            if row.flat_amount_minor is not None
            else None
        ),
        unit_amount=(
            Money(row.unit_amount_minor, row.currency)
            if row.unit_amount_minor is not None
            else None
        ),
        tiers=tiers,
        tier_mode=row.tier_mode,
        meter=row.meter,
        aggregation=row.aggregation,
        included_units=row.included_units,
        nickname=row.nickname,
    )


def plan_from_row(plan: BillingPlan, prices: list[BillingPrice]) -> Plan:
    """Convert a :class:`BillingPlan` + its price rows to a catalog :class:`Plan`."""
    features: tuple[Feature, ...] = ()
    if plan.features:
        features = tuple(
            Feature(key=f["key"], label=f.get("label", f["key"]), limit=f.get("limit"))
            for f in plan.features
        )
    # Price is a frozen dataclass; rebuild each with the owning plan code set.
    catalog_prices = tuple(_with_plan_code(price_from_row(p), plan.code) for p in prices)
    return Plan(
        code=plan.code,
        name=plan.name,
        tier=plan.tier,
        prices=catalog_prices,
        features=features,
        trial_days=plan.trial_days,
        active=plan.active,
        description=plan.description,
        metadata=dict(plan.plan_metadata or {}),
    )


def _with_plan_code(price: Price, plan_code: str) -> Price:
    """Return a copy of ``price`` with ``plan_code`` set (frozen dataclass)."""
    return Price(
        id=price.id,
        plan_code=plan_code,
        type=price.type,
        interval=price.interval,
        currency=price.currency,
        flat_amount=price.flat_amount,
        unit_amount=price.unit_amount,
        tiers=price.tiers,
        tier_mode=price.tier_mode,
        meter=price.meter,
        aggregation=price.aggregation,
        included_units=price.included_units,
        nickname=price.nickname,
    )


def coupon_from_row(row: BillingCoupon) -> Coupon:
    """Convert a :class:`BillingCoupon` ORM row to a pure :class:`Coupon`."""
    from decimal import Decimal

    return Coupon(
        code=row.code,
        discount_type=row.discount_type,
        percent_off=Decimal(str(row.percent_off)) if row.percent_off is not None else None,
        amount_off=(
            Money(row.amount_off_minor, row.currency or "USD")
            if row.amount_off_minor is not None
            else None
        ),
        duration=row.duration,
        duration_in_periods=row.duration_in_periods,
        max_redemptions=row.max_redemptions,
        redeemed_count=row.redeemed_count,
        redeem_by=row.redeem_by,
        min_subtotal_minor=row.min_subtotal_minor,
        active=row.active,
    )


__all__ = ["coupon_from_row", "plan_from_row", "price_from_row"]
