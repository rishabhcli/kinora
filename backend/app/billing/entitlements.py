"""Entitlements + feature gating — the commercial guardrail.

A plan grants **entitlements**: a set of boolean/limited *features* (PDF upload,
Director mode, voice cloning, priority render lane, library size) and per-meter
*allowances* (the included render-seconds / reading-minutes baked into the plan's
metered prices). This module projects a :class:`app.billing.catalog.Plan` into an
:class:`Entitlements` snapshot and exposes the gate the rest of the app calls:

* :meth:`Entitlements.require_feature` — "is Director mode available on this
  plan?" (raises :class:`EntitlementDeniedError` with HTTP 402 if not).
* :meth:`Entitlements.check_quota` / :meth:`require_within_quota` — "would this
  push reading-minutes / render-seconds / books past the included allowance?"

The gate is conceptually tied to the §11 budget: the render-seconds allowance is
the *commercial* envelope around the same scarce video-seconds the budget ledger
caps. A reader who is over their plan's render-seconds is offered an upgrade
(402) rather than silently spending more provider budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.billing.catalog import Plan, Price
from app.billing.enums import PlanTier, PriceType, UsageMeter
from app.billing.errors import EntitlementDeniedError

#: Tier ordering for "required tier" messaging and upgrade comparisons.
TIER_ORDER: tuple[PlanTier, ...] = (
    PlanTier.FREE,
    PlanTier.STARTER,
    PlanTier.PRO,
    PlanTier.STUDIO,
    PlanTier.ENTERPRISE,
)


def tier_rank(tier: PlanTier) -> int:
    """Ordinal rank of a tier (FREE=0 .. ENTERPRISE=4)."""
    return TIER_ORDER.index(tier)


@dataclass(frozen=True, slots=True)
class FeatureGrant:
    """A granted feature with an optional numeric quota.

    ``limit=None`` means unlimited; ``limit=0`` is the "locked / not included"
    sentinel (present in the catalog only to render a disabled UI row). The gate
    treats ``limit=0`` as denied.
    """

    key: str
    limit: float | None

    @property
    def is_unlimited(self) -> bool:
        return self.limit is None

    @property
    def is_locked(self) -> bool:
        return self.limit == 0


@dataclass(frozen=True, slots=True)
class MeterAllowance:
    """The included (free) allowance for a metered meter, per period."""

    meter: UsageMeter
    included_units: int


@dataclass
class Entitlements:
    """A flattened snapshot of what a plan grants (the gate's source of truth)."""

    tier: PlanTier
    plan_code: str
    features: dict[str, FeatureGrant] = field(default_factory=dict)
    allowances: dict[UsageMeter, MeterAllowance] = field(default_factory=dict)
    active: bool = True

    # -- feature gates ------------------------------------------------------- #

    def has_feature(self, key: str) -> bool:
        """True when the feature is present and not the ``limit=0`` locked sentinel."""
        grant = self.features.get(key)
        return grant is not None and not grant.is_locked

    def require_feature(self, key: str, *, required_tier: PlanTier | None = None) -> None:
        """Raise :class:`EntitlementDeniedError` (402) if ``key`` is not granted."""
        if not self.active:
            raise EntitlementDeniedError(
                f"subscription is not active; '{key}' requires an active plan",
                feature=key,
            )
        if not self.has_feature(key):
            raise EntitlementDeniedError(
                f"feature '{key}' is not included in the {self.tier.value} plan",
                feature=key,
                required_tier=(required_tier.value if required_tier else None),
            )

    def feature_limit(self, key: str) -> float | None:
        """The numeric quota for a feature (None => unlimited or absent)."""
        grant = self.features.get(key)
        return grant.limit if grant is not None else None

    # -- quota / allowance gates -------------------------------------------- #

    def included_units(self, meter: UsageMeter) -> int:
        """The included per-period allowance for ``meter`` (0 if none)."""
        allowance = self.allowances.get(meter)
        return allowance.included_units if allowance is not None else 0

    def overage(self, meter: UsageMeter, used: float) -> float:
        """How far ``used`` exceeds the included allowance (0 if within)."""
        return max(0.0, used - self.included_units(meter))

    def check_feature_quota(self, key: str, *, count: float) -> bool:
        """Whether ``count`` is within the feature's numeric quota (True if ok).

        ``limit=None`` (unlimited) always passes; a locked feature always fails.
        """
        grant = self.features.get(key)
        if grant is None or grant.is_locked:
            return False
        if grant.is_unlimited:
            return True
        assert grant.limit is not None
        return count <= grant.limit

    def require_feature_quota(self, key: str, *, count: float) -> None:
        """Raise 402 if ``count`` exceeds the feature's quota."""
        if not self.check_feature_quota(key, count=count):
            limit = self.feature_limit(key)
            raise EntitlementDeniedError(
                f"'{key}' limit reached on the {self.tier.value} plan "
                f"({count:g} requested, limit {limit})",
                feature=key,
                limit=limit,
                used=count,
            )


def project_entitlements(plan: Plan, *, active: bool = True) -> Entitlements:
    """Flatten a :class:`Plan` into an :class:`Entitlements` snapshot.

    Features come from the plan's feature list; meter allowances come from the
    ``included_units`` on the plan's metered prices.
    """
    features = {f.key: FeatureGrant(key=f.key, limit=f.limit) for f in plan.features}
    allowances: dict[UsageMeter, MeterAllowance] = {}
    for price in plan.prices:
        if price.type is PriceType.METERED and price.meter is not None:
            # If two metered prices share a meter, the larger allowance wins.
            existing = allowances.get(price.meter)
            if existing is None or price.included_units > existing.included_units:
                allowances[price.meter] = MeterAllowance(
                    meter=price.meter, included_units=price.included_units
                )
    return Entitlements(
        tier=plan.tier,
        plan_code=plan.code,
        features=features,
        allowances=allowances,
        active=active,
    )


def metered_prices(plan: Plan) -> list[Price]:
    """The plan's metered prices (the overage prices the invoicer bills)."""
    return [p for p in plan.prices if p.type is PriceType.METERED]


__all__ = [
    "TIER_ORDER",
    "Entitlements",
    "FeatureGrant",
    "MeterAllowance",
    "metered_prices",
    "project_entitlements",
    "tier_rank",
]
