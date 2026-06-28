"""Tests for entitlements projection + feature/quota gating."""

from __future__ import annotations

import pytest

from app.billing.default_catalog import (
    FEATURE_DIRECTOR_MODE,
    FEATURE_MAX_BOOKS,
    FEATURE_PDF_UPLOAD,
    FEATURE_VOICE_CLONE,
    FREE_PLAN,
    PRO_PLAN,
    STARTER_PLAN,
    STUDIO_PLAN,
)
from app.billing.entitlements import (
    TIER_ORDER,
    project_entitlements,
    tier_rank,
)
from app.billing.enums import PlanTier, UsageMeter
from app.billing.errors import EntitlementDeniedError


def test_tier_order_and_rank() -> None:
    assert tier_rank(PlanTier.FREE) == 0
    assert tier_rank(PlanTier.STUDIO) == 3
    assert tier_rank(PlanTier.ENTERPRISE) > tier_rank(PlanTier.PRO)
    assert len(TIER_ORDER) == 5


def test_free_plan_locks_director_mode() -> None:
    ent = project_entitlements(FREE_PLAN)
    # director_mode is present with limit=0 (locked sentinel).
    assert not ent.has_feature(FEATURE_DIRECTOR_MODE)
    with pytest.raises(EntitlementDeniedError) as exc:
        ent.require_feature(FEATURE_DIRECTOR_MODE, required_tier=PlanTier.STARTER)
    assert exc.value.http_status == 402
    assert exc.value.feature == FEATURE_DIRECTOR_MODE
    assert exc.value.required_tier == "starter"


def test_starter_grants_director_mode() -> None:
    ent = project_entitlements(STARTER_PLAN)
    assert ent.has_feature(FEATURE_DIRECTOR_MODE)
    ent.require_feature(FEATURE_DIRECTOR_MODE)  # no raise


def test_inactive_subscription_denies_features() -> None:
    ent = project_entitlements(PRO_PLAN, active=False)
    with pytest.raises(EntitlementDeniedError):
        ent.require_feature(FEATURE_VOICE_CLONE)


def test_render_seconds_allowance_projected() -> None:
    starter = project_entitlements(STARTER_PLAN)
    assert starter.included_units(UsageMeter.RENDER_SECONDS) == 300
    pro = project_entitlements(PRO_PLAN)
    assert pro.included_units(UsageMeter.RENDER_SECONDS) == 1200
    studio = project_entitlements(STUDIO_PLAN)
    assert studio.included_units(UsageMeter.RENDER_SECONDS) == 6000


def test_overage_calc() -> None:
    starter = project_entitlements(STARTER_PLAN)
    assert starter.overage(UsageMeter.RENDER_SECONDS, 250) == 0.0  # within
    assert starter.overage(UsageMeter.RENDER_SECONDS, 500) == 200.0  # 200 over


def test_no_allowance_meter_returns_zero() -> None:
    free = project_entitlements(FREE_PLAN)
    assert free.included_units(UsageMeter.RENDER_SECONDS) == 0
    assert free.overage(UsageMeter.RENDER_SECONDS, 10) == 10.0


def test_feature_quota_limited() -> None:
    free = project_entitlements(FREE_PLAN)
    # max_books limit 3 on Free.
    assert free.check_feature_quota(FEATURE_MAX_BOOKS, count=3)
    assert not free.check_feature_quota(FEATURE_MAX_BOOKS, count=4)
    with pytest.raises(EntitlementDeniedError) as exc:
        free.require_feature_quota(FEATURE_MAX_BOOKS, count=4)
    assert exc.value.limit == 3
    assert exc.value.used == 4


def test_feature_quota_unlimited_passes() -> None:
    pro = project_entitlements(PRO_PLAN)
    # PDF upload is unlimited (limit=None) on Pro.
    assert pro.check_feature_quota(FEATURE_PDF_UPLOAD, count=99999)
    pro.require_feature_quota(FEATURE_PDF_UPLOAD, count=99999)  # no raise


def test_feature_quota_missing_feature_fails() -> None:
    free = project_entitlements(FREE_PLAN)
    assert not free.check_feature_quota(FEATURE_VOICE_CLONE, count=1)


def test_larger_allowance_wins_when_meter_repeats() -> None:
    from app.billing.catalog import Plan, Price
    from app.billing.enums import BillingInterval, MeteredAggregation, PriceType
    from app.billing.money import Money

    def metered(price_id: str, included: int) -> Price:
        return Price(
            id=price_id,
            plan_code="x",
            type=PriceType.METERED,
            interval=BillingInterval.MONTH,
            meter=UsageMeter.RENDER_SECONDS,
            aggregation=MeteredAggregation.SUM,
            included_units=included,
            unit_amount=Money(1),
        )

    plan = Plan(
        code="x",
        name="X",
        tier=PlanTier.PRO,
        prices=(metered("a", 100), metered("b", 500)),
    )
    ent = project_entitlements(plan)
    assert ent.included_units(UsageMeter.RENDER_SECONDS) == 500
