"""Tests for the plan/price catalog and pure pricing math (app.billing.catalog)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.billing.catalog import (
    Feature,
    Plan,
    Price,
    PriceTier,
    aggregate_usage,
    annualized,
    compute_price_charge,
    compute_tiered_charge,
    discount_percent_off,
)
from app.billing.default_catalog import (
    FEATURE_DIRECTOR_MODE,
    PRO_PLAN,
    STARTER_PLAN,
    STUDIO_PLAN,
    default_plans,
    plan_by_code,
)
from app.billing.enums import (
    BillingInterval,
    MeteredAggregation,
    PlanTier,
    PriceType,
    TierMode,
    UsageMeter,
)
from app.billing.errors import PriceNotFoundError
from app.billing.money import Money

# --- Price validation ------------------------------------------------------- #


def test_flat_price_requires_amount() -> None:
    with pytest.raises(ValueError):
        Price(id="p", plan_code="x", type=PriceType.FLAT, interval=BillingInterval.MONTH)


def test_metered_price_requires_meter() -> None:
    with pytest.raises(ValueError):
        Price(
            id="p",
            plan_code="x",
            type=PriceType.METERED,
            interval=BillingInterval.MONTH,
            unit_amount=Money(1),
        )


def test_tier_bounds_must_ascend() -> None:
    with pytest.raises(ValueError):
        Price(
            id="p",
            plan_code="x",
            type=PriceType.PER_UNIT,
            interval=BillingInterval.MONTH,
            tiers=(
                PriceTier(up_to=100, unit_amount=Money(1)),
                PriceTier(up_to=50, unit_amount=Money(1)),  # not ascending
            ),
        )


def test_only_final_tier_unbounded() -> None:
    with pytest.raises(ValueError):
        Price(
            id="p",
            plan_code="x",
            type=PriceType.PER_UNIT,
            interval=BillingInterval.MONTH,
            tiers=(
                PriceTier(up_to=None, unit_amount=Money(1)),  # unbounded not last
                PriceTier(up_to=100, unit_amount=Money(1)),
            ),
        )


# --- Flat / per-unit charging ----------------------------------------------- #


def test_flat_charge_ignores_quantity() -> None:
    price = Price(
        id="p",
        plan_code="x",
        type=PriceType.FLAT,
        interval=BillingInterval.MONTH,
        flat_amount=Money(900),
    )
    assert compute_price_charge(price, 0).amount_minor == 900
    assert compute_price_charge(price, 999).amount_minor == 900


def test_per_unit_charge() -> None:
    price = Price(
        id="p",
        plan_code="x",
        type=PriceType.PER_UNIT,
        interval=BillingInterval.MONTH,
        unit_amount=Money(500),
    )
    assert compute_price_charge(price, 3).amount_minor == 1500


def test_included_units_subtracted() -> None:
    price = Price(
        id="p",
        plan_code="x",
        type=PriceType.METERED,
        interval=BillingInterval.MONTH,
        meter=UsageMeter.RENDER_SECONDS,
        unit_amount=Money(2),
        included_units=300,
    )
    # 250 used, all included -> 0; 400 used -> 100 billable * 2¢ = 200.
    assert compute_price_charge(price, 250).amount_minor == 0
    assert compute_price_charge(price, 400).amount_minor == 200


# --- Tiered pricing --------------------------------------------------------- #


def test_graduated_tiers() -> None:
    tiers = (
        PriceTier(up_to=10, unit_amount=Money(100)),  # first 10 @ $1.00
        PriceTier(up_to=20, unit_amount=Money(50)),  # next 10 @ $0.50
        PriceTier(up_to=None, unit_amount=Money(25)),  # rest @ $0.25
    )
    # 25 units: 10*100 + 10*50 + 5*25 = 1000 + 500 + 125 = 1625.
    assert compute_tiered_charge(tiers, 25, TierMode.GRADUATED, "USD").amount_minor == 1625
    # 5 units: only first tier -> 500.
    assert compute_tiered_charge(tiers, 5, TierMode.GRADUATED, "USD").amount_minor == 500
    # exactly 10 -> 1000.
    assert compute_tiered_charge(tiers, 10, TierMode.GRADUATED, "USD").amount_minor == 1000


def test_graduated_with_flat_per_tier() -> None:
    tiers = (
        PriceTier(up_to=10, unit_amount=Money(100), flat_amount=Money(200)),
        PriceTier(up_to=None, unit_amount=Money(50), flat_amount=Money(100)),
    )
    # 15 units: tier1 10*100 + 200 flat = 1200; tier2 5*50 + 100 flat = 350 -> 1550.
    assert compute_tiered_charge(tiers, 15, TierMode.GRADUATED, "USD").amount_minor == 1550


def test_volume_tiers() -> None:
    tiers = (
        PriceTier(up_to=10, unit_amount=Money(100)),
        PriceTier(up_to=100, unit_amount=Money(80)),
        PriceTier(up_to=None, unit_amount=Money(50)),
    )
    # Volume: 50 units land in the 80¢ tier -> 50*80 = 4000.
    assert compute_tiered_charge(tiers, 50, TierMode.VOLUME, "USD").amount_minor == 4000
    # 5 units land in tier1 -> 5*100 = 500.
    assert compute_tiered_charge(tiers, 5, TierMode.VOLUME, "USD").amount_minor == 500
    # 500 units land in the unbounded tier -> 500*50 = 25000.
    assert compute_tiered_charge(tiers, 500, TierMode.VOLUME, "USD").amount_minor == 25000


def test_tiered_zero_quantity() -> None:
    tiers = (PriceTier(up_to=None, unit_amount=Money(100)),)
    assert compute_tiered_charge(tiers, 0, TierMode.GRADUATED, "USD").is_zero


def test_tiered_negative_raises() -> None:
    tiers = (PriceTier(up_to=None, unit_amount=Money(100)),)
    with pytest.raises(ValueError):
        compute_tiered_charge(tiers, -1, TierMode.GRADUATED, "USD")


def test_charge_negative_quantity_raises() -> None:
    price = Price(
        id="p",
        plan_code="x",
        type=PriceType.PER_UNIT,
        interval=BillingInterval.MONTH,
        unit_amount=Money(100),
    )
    with pytest.raises(ValueError):
        compute_price_charge(price, -1)


# --- Aggregation ------------------------------------------------------------ #


def test_aggregate_usage_modes() -> None:
    assert aggregate_usage([1.0, 2.0, 3.0], MeteredAggregation.SUM) == 6.0
    assert aggregate_usage([1.0, 5.0, 3.0], MeteredAggregation.MAX) == 5.0
    assert aggregate_usage([1.0, 5.0, 3.0], MeteredAggregation.LAST) == 3.0
    assert aggregate_usage([], MeteredAggregation.SUM) == 0.0


# --- Helpers ---------------------------------------------------------------- #


def test_annualized() -> None:
    assert annualized(Money(900), BillingInterval.MONTH).amount_minor == 10800
    assert annualized(Money(29000), BillingInterval.YEAR).amount_minor == 29000


def test_discount_percent_off() -> None:
    assert discount_percent_off(Money(10000), 20).amount_minor == 2000
    assert discount_percent_off(Money(999), Decimal("33.5")).amount_minor == 335
    with pytest.raises(ValueError):
        discount_percent_off(Money(100), 150)


# --- Plan lookups ----------------------------------------------------------- #


def test_plan_price_lookup() -> None:
    price = PRO_PLAN.price("price_pro_month")
    assert price.flat_amount is not None and price.flat_amount.amount_minor == 2900
    with pytest.raises(PriceNotFoundError):
        PRO_PLAN.price("nope")


def test_plan_primary_price_by_interval() -> None:
    yearly = PRO_PLAN.primary_price(BillingInterval.YEAR)
    assert yearly is not None and yearly.id == "price_pro_year"


def test_plan_feature_helpers() -> None:
    assert STARTER_PLAN.has_feature(FEATURE_DIRECTOR_MODE)
    assert STARTER_PLAN.feature(FEATURE_DIRECTOR_MODE) is not None
    assert not STARTER_PLAN.has_feature("does_not_exist")


def test_default_catalog_lookups() -> None:
    codes = {p.code for p in default_plans()}
    assert codes == {"free", "starter", "pro", "studio"}
    assert plan_by_code("studio") is STUDIO_PLAN
    assert plan_by_code("nope") is None
    assert STUDIO_PLAN.tier is PlanTier.STUDIO


def test_pro_graduated_overage_realistic() -> None:
    # Pro includes 1200 render-s; reader used 1200 + 2000 overage = 3200.
    overage_price = PRO_PLAN.price("price_pro_render_overage")
    # billable overage = 3200 - 1200 = 2000; first 1800 @2¢, next 200 @1¢.
    charge = compute_price_charge(overage_price, 3200)
    assert charge.amount_minor == 1800 * 2 + 200 * 1  # 3600 + 200 = 3800


def test_feature_with_zero_limit_is_locked_sentinel() -> None:
    # Free's director_mode has limit=0 — present but quota-zero (a "locked" gate
    # convention exercised by the entitlements tests).
    f = Plan(code="x", name="X", tier=PlanTier.FREE, features=(Feature("d", "D", limit=0),))
    assert f.feature("d") is not None and f.feature("d").limit == 0
