"""The default Kinora plan catalog.

Concrete plans wiring the abstract :mod:`app.billing.catalog` types into the
product's actual tiers. These are the *seed* catalog the service uses when the DB
has no plans persisted yet (and what the tests price against). They deliberately
mirror the §11 budget framing:

* every paid tier grants a monthly **render-seconds** allowance (the commercial
  envelope around the scarce video-seconds the budget ledger meters), then bills
  metered overage per render-second;
* **reading-minutes** are unlimited on paid tiers and capped on Free;
* feature gates (PDF upload, Director mode, voice cloning, priority render lane)
  follow the tier.

Amounts are illustrative but internally consistent. Nothing here charges or
spends anything — it is data.
"""

from __future__ import annotations

from app.billing.catalog import Feature, Plan, Price, PriceTier
from app.billing.enums import (
    BillingInterval,
    MeteredAggregation,
    PlanTier,
    PriceType,
    TierMode,
    UsageMeter,
)
from app.billing.money import Money

# Feature keys (referenced by the entitlements gate).
FEATURE_PDF_UPLOAD = "pdf_upload"
FEATURE_DIRECTOR_MODE = "director_mode"
FEATURE_VOICE_CLONE = "voice_clone"
FEATURE_PRIORITY_RENDER = "priority_render"
FEATURE_MAX_BOOKS = "max_books"
FEATURE_LIVE_VIDEO = "live_video"


def _usd(major: str) -> Money:
    return Money.from_major(major, "USD")


FREE_PLAN = Plan(
    code="free",
    name="Free",
    tier=PlanTier.FREE,
    trial_days=0,
    description="Read along with Ken-Burns animatics; no live video.",
    prices=(
        Price(
            id="price_free_month",
            plan_code="free",
            type=PriceType.FLAT,
            interval=BillingInterval.MONTH,
            flat_amount=Money.zero("USD"),
        ),
    ),
    features=(
        Feature(FEATURE_PDF_UPLOAD, "Upload your own PDFs", limit=3),
        Feature(FEATURE_MAX_BOOKS, "Library size", limit=3),
        Feature(FEATURE_DIRECTOR_MODE, "Director mode", limit=0),  # locked
    ),
)


STARTER_PLAN = Plan(
    code="starter",
    name="Starter",
    tier=PlanTier.STARTER,
    trial_days=7,
    description="Live video for one book at a time, with a monthly render budget.",
    prices=(
        Price(
            id="price_starter_month",
            plan_code="starter",
            type=PriceType.FLAT,
            interval=BillingInterval.MONTH,
            flat_amount=_usd("9.00"),
        ),
        Price(
            id="price_starter_render_overage",
            plan_code="starter",
            type=PriceType.METERED,
            interval=BillingInterval.MONTH,
            meter=UsageMeter.RENDER_SECONDS,
            aggregation=MeteredAggregation.SUM,
            included_units=300,  # 5 min of accepted film/month included
            unit_amount=Money(2, "USD"),  # 2¢ / render-second overage
        ),
    ),
    features=(
        Feature(FEATURE_PDF_UPLOAD, "Upload your own PDFs", limit=25),
        Feature(FEATURE_MAX_BOOKS, "Library size", limit=25),
        Feature(FEATURE_DIRECTOR_MODE, "Director mode"),
        Feature(FEATURE_LIVE_VIDEO, "Live AI video"),
    ),
)


PRO_PLAN = Plan(
    code="pro",
    name="Pro",
    tier=PlanTier.PRO,
    trial_days=14,
    description="More render budget, graduated overage, voice cloning.",
    prices=(
        Price(
            id="price_pro_month",
            plan_code="pro",
            type=PriceType.FLAT,
            interval=BillingInterval.MONTH,
            flat_amount=_usd("29.00"),
        ),
        Price(
            id="price_pro_year",
            plan_code="pro",
            type=PriceType.FLAT,
            interval=BillingInterval.YEAR,
            flat_amount=_usd("290.00"),  # ~2 months free
        ),
        Price(
            id="price_pro_render_overage",
            plan_code="pro",
            type=PriceType.METERED,
            interval=BillingInterval.MONTH,
            meter=UsageMeter.RENDER_SECONDS,
            aggregation=MeteredAggregation.SUM,
            included_units=1200,  # 20 min of accepted film/month included
            tier_mode=TierMode.GRADUATED,
            tiers=(
                # First 1800 overage render-seconds at 1.5¢, then 1¢ thereafter.
                PriceTier(up_to=1800, unit_amount=Money(2, "USD")),
                PriceTier(up_to=None, unit_amount=Money(1, "USD")),
            ),
        ),
    ),
    features=(
        Feature(FEATURE_PDF_UPLOAD, "Upload your own PDFs"),
        Feature(FEATURE_MAX_BOOKS, "Library size", limit=500),
        Feature(FEATURE_DIRECTOR_MODE, "Director mode"),
        Feature(FEATURE_VOICE_CLONE, "Voice cloning"),
        Feature(FEATURE_PRIORITY_RENDER, "Priority render lane"),
        Feature(FEATURE_LIVE_VIDEO, "Live AI video"),
    ),
)


STUDIO_PLAN = Plan(
    code="studio",
    name="Studio",
    tier=PlanTier.STUDIO,
    trial_days=14,
    description="Volume render pricing for power users and small teams.",
    prices=(
        Price(
            id="price_studio_month",
            plan_code="studio",
            type=PriceType.FLAT,
            interval=BillingInterval.MONTH,
            flat_amount=_usd("99.00"),
        ),
        Price(
            id="price_studio_render_overage",
            plan_code="studio",
            type=PriceType.METERED,
            interval=BillingInterval.MONTH,
            meter=UsageMeter.RENDER_SECONDS,
            aggregation=MeteredAggregation.SUM,
            included_units=6000,  # 100 min/month included
            tier_mode=TierMode.VOLUME,
            tiers=(
                PriceTier(up_to=6000, unit_amount=Money(1, "USD")),
                PriceTier(up_to=None, unit_amount=Money(1, "USD"), flat_amount=Money(0, "USD")),
            ),
        ),
    ),
    features=(
        Feature(FEATURE_PDF_UPLOAD, "Upload your own PDFs"),
        Feature(FEATURE_MAX_BOOKS, "Library size"),
        Feature(FEATURE_DIRECTOR_MODE, "Director mode"),
        Feature(FEATURE_VOICE_CLONE, "Voice cloning"),
        Feature(FEATURE_PRIORITY_RENDER, "Priority render lane"),
        Feature(FEATURE_LIVE_VIDEO, "Live AI video"),
    ),
)


#: The default plans in tier order.
DEFAULT_PLANS: tuple[Plan, ...] = (FREE_PLAN, STARTER_PLAN, PRO_PLAN, STUDIO_PLAN)


def default_plans() -> tuple[Plan, ...]:
    """Return the seed plan catalog (immutable)."""
    return DEFAULT_PLANS


def plan_by_code(code: str) -> Plan | None:
    """Look up a default plan by its code."""
    for plan in DEFAULT_PLANS:
        if plan.code == code:
            return plan
    return None


__all__ = [
    "DEFAULT_PLANS",
    "FEATURE_DIRECTOR_MODE",
    "FEATURE_LIVE_VIDEO",
    "FEATURE_MAX_BOOKS",
    "FEATURE_PDF_UPLOAD",
    "FEATURE_PRIORITY_RENDER",
    "FEATURE_VOICE_CLONE",
    "FREE_PLAN",
    "PRO_PLAN",
    "STARTER_PLAN",
    "STUDIO_PLAN",
    "default_plans",
    "plan_by_code",
]
