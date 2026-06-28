"""Persist the default plan catalog into the billing tables (idempotent).

Turns the in-memory :data:`app.billing.default_catalog.DEFAULT_PLANS` into
``billing_plans`` + ``billing_prices`` rows. Safe to run repeatedly: a plan whose
``code`` already exists is skipped, so seeding on every boot is a no-op after the
first. Prices keep their stable catalog ids (``price_pro_month`` …) so a
subscription can reference a price id directly.
"""

from __future__ import annotations

from app.billing.catalog import Plan, Price
from app.billing.default_catalog import DEFAULT_PLANS
from app.billing.models import BillingPlan, BillingPrice
from app.billing.repositories import PlanRepo


def _price_row(price: Price) -> BillingPrice:
    tiers = None
    if price.tiers:
        tiers = [
            {
                "up_to": t.up_to,
                "unit_amount_minor": t.unit_amount.amount_minor,
                "flat_amount_minor": (
                    t.flat_amount.amount_minor if t.flat_amount is not None else None
                ),
            }
            for t in price.tiers
        ]
    return BillingPrice(
        id=price.id,
        plan_id="",  # set by the caller after the plan is flushed
        type=price.type,
        interval=price.interval,
        currency=price.currency,
        flat_amount_minor=price.flat_amount.amount_minor if price.flat_amount else None,
        unit_amount_minor=price.unit_amount.amount_minor if price.unit_amount else None,
        tiers=tiers,
        tier_mode=price.tier_mode,
        meter=price.meter,
        aggregation=price.aggregation,
        included_units=price.included_units,
        nickname=price.nickname,
    )


def _plan_row(plan: Plan) -> BillingPlan:
    return BillingPlan(
        code=plan.code,
        name=plan.name,
        tier=plan.tier,
        trial_days=plan.trial_days,
        active=plan.active,
        description=plan.description,
        features=[{"key": f.key, "label": f.label, "limit": f.limit} for f in plan.features],
        plan_metadata=dict(plan.metadata) or None,
    )


async def seed_default_catalog(repo: PlanRepo) -> int:
    """Persist any default plans that don't exist yet; return how many created."""
    created = 0
    for plan in DEFAULT_PLANS:
        if await repo.get_by_code(plan.code) is not None:
            continue
        row = await repo.create(_plan_row(plan))
        for price in plan.prices:
            price_row = _price_row(price)
            price_row.plan_id = row.id
            await repo.add_price(price_row)
        created += 1
    return created


__all__ = ["seed_default_catalog"]
