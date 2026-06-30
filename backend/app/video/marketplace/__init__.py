"""Video-provider **marketplace**: a curated, queryable model catalog + onboarding.

Kinora's mandate is *any-model* support. This subsystem is the curated front
door to that: a rich, searchable catalog of hosted video models and a
programmatic onboarding flow to bring new ones in safely. It is **additive and
fully self-contained** — it owns its own capability/profile vocabulary
(:mod:`app.video.marketplace.types`), an in-memory catalog, an onboarding state
machine, and a deprecation/sunset lifecycle, with no dependency on any other
Kinora subsystem (Round-3 FINAL: earlier provider-registry rounds are not
merged, so nothing here imports them).

Layers (bottom-up):

* :mod:`.types` — the owned vocabulary (Modality, Capability, Maturity, …).
* :mod:`.listing` — the rich :class:`~app.video.marketplace.listing.ModelListing`
  value object (pricing tiers, region availability, license/ToS, samples,
  reputation pointer, status) + validators.
* :mod:`.catalog` — the searchable / filterable / rankable in-memory catalog,
  plus a structured two-model compare.
* :mod:`.onboarding` — a reversible, explainable wizard state machine
  (declare → validate → conformance dry-run → configure → stage → activate).
* :mod:`.lifecycle` — deprecation / sunset / retire with migration hints.
* :mod:`.seed` — a deterministic curated starter catalog (Wan, MiniMax, …).
* :mod:`.service` — the read-mostly facade the API talks to.
* :mod:`.api` — the read-only catalog router (mounted at ``/video/marketplace``).

Nothing here spends credits, calls a provider, or depends on
``KINORA_LIVE_VIDEO`` — it is descriptive metadata + pure policy.
"""

from __future__ import annotations

from app.video.marketplace.catalog import (
    CatalogQuery,
    Comparison,
    ModelCatalog,
    RankWeights,
    ScoredListing,
)
from app.video.marketplace.errors import (
    GateFailedError,
    InvalidTransitionError,
    LifecycleError,
    ListingNotFoundError,
    ListingValidationError,
    MarketplaceError,
)
from app.video.marketplace.lifecycle import (
    LifecycleEvent,
    LifecycleManager,
    MigrationHint,
)
from app.video.marketplace.listing import (
    ModelListing,
    PricingTier,
    RegionAvailability,
    ReputationPointer,
    SampleRef,
)
from app.video.marketplace.onboarding import (
    GateResult,
    OnboardingStage,
    OnboardingWizard,
    default_conformance_probe,
)
from app.video.marketplace.seed import seed_catalog, seed_listings
from app.video.marketplace.service import MarketplaceService
from app.video.marketplace.types import (
    Capability,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    PricingModel,
    Region,
)

__all__ = [
    "CatalogQuery",
    "Capability",
    "Comparison",
    "GateFailedError",
    "GateResult",
    "InvalidTransitionError",
    "LicenseClass",
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleManager",
    "ListingNotFoundError",
    "ListingStatus",
    "ListingValidationError",
    "MarketplaceError",
    "MarketplaceService",
    "Maturity",
    "MigrationHint",
    "ModelCatalog",
    "ModelListing",
    "Modality",
    "OnboardingStage",
    "OnboardingWizard",
    "PricingModel",
    "PricingTier",
    "RankWeights",
    "Region",
    "RegionAvailability",
    "ReputationPointer",
    "SampleRef",
    "ScoredListing",
    "default_conformance_probe",
    "seed_catalog",
    "seed_listings",
]
