"""The marketplace facade: one object the API (and any host) talks to.

:class:`MarketplaceService` wires together the three pieces — the in-memory
:class:`~app.video.marketplace.catalog.ModelCatalog`, the onboarding
:class:`~app.video.marketplace.onboarding.OnboardingWizard`, and the
:class:`~app.video.marketplace.lifecycle.LifecycleManager` — behind a small,
read-mostly surface. The read-only API maps directly onto its query methods;
onboarding/lifecycle methods exist for completeness and tests but are not
exposed by the read-only router.

The service is **self-contained**: it owns its catalog (seeded by default) and
never touches a DB, Redis, network, or another Kinora subsystem. Construction is
cheap and deterministic, so a host can build one per process from the seed.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from app.video.marketplace.catalog import (
    CatalogQuery,
    Comparison,
    ModelCatalog,
    RankWeights,
    ScoredListing,
)
from app.video.marketplace.lifecycle import LifecycleEvent, LifecycleManager, MigrationHint
from app.video.marketplace.listing import ModelListing
from app.video.marketplace.onboarding import (
    ConformanceProbe,
    GateResult,
    OnboardingWizard,
    default_conformance_probe,
)
from app.video.marketplace.seed import seed_catalog
from app.video.marketplace.types import ListingStatus

logger = structlog.get_logger("app.video.marketplace.service")


class MarketplaceService:
    """Read-mostly facade over the catalog + onboarding + lifecycle."""

    def __init__(
        self,
        catalog: ModelCatalog | None = None,
        *,
        conformance_probe: ConformanceProbe | None = None,
    ) -> None:
        self._catalog = catalog if catalog is not None else seed_catalog()
        self._lifecycle = LifecycleManager(self._catalog)
        self._probe = conformance_probe or default_conformance_probe

    @property
    def catalog(self) -> ModelCatalog:
        return self._catalog

    @property
    def lifecycle(self) -> LifecycleManager:
        return self._lifecycle

    # ------------------------------- reads ------------------------------ #
    def list_listings(self, *, include_retired: bool = False) -> list[ModelListing]:
        """Every listing, default-visible unless ``include_retired``."""
        return [
            li
            for li in self._catalog.all()
            if include_retired or li.status.is_visible_by_default
        ]

    def get(self, key: str) -> ModelListing:
        """Fetch one listing by key (raises ``ListingNotFoundError``)."""
        return self._catalog.get(key)

    def search(
        self, query: CatalogQuery | None = None, *, weights: RankWeights | None = None
    ) -> list[ScoredListing]:
        """Filter + rank (delegates to the catalog)."""
        return self._catalog.search(query, weights=weights)

    def compare(self, left_key: str, right_key: str) -> Comparison:
        """Side-by-side of two listings."""
        return self._catalog.compare(left_key, right_key)

    def migration_hint(self, key: str) -> MigrationHint:
        """Migration guidance for a (typically deprecated) listing."""
        return self._lifecycle.migration_hint(key)

    # --------------------------- onboarding ----------------------------- #
    def begin_onboarding(self, listing: ModelListing) -> OnboardingWizard:
        """Start a wizard for ``listing`` using the service's conformance probe."""
        return OnboardingWizard.declare(listing, probe=self._probe)

    def onboard(
        self,
        listing: ModelListing,
        *,
        require_pricing: bool = True,
        require_region: bool = True,
        require_commercial_license: bool = False,
    ) -> tuple[OnboardingWizard, list[GateResult]]:
        """Run the full wizard; on full success the activated listing is upserted.

        Returns the wizard (with its explainable history) and the gate results.
        The catalog is only mutated when the listing reaches ``ACTIVE`` or
        ``PREVIEW`` — failed onboarding leaves the catalog untouched.
        """
        wiz = self.begin_onboarding(listing)
        results = wiz.run_all(
            require_pricing=require_pricing,
            require_region=require_region,
            require_commercial_license=require_commercial_license,
        )
        if wiz.listing.status in (ListingStatus.ACTIVE, ListingStatus.PREVIEW):
            self._catalog.upsert(wiz.listing)
            logger.info(
                "marketplace.onboarded",
                key=wiz.listing.key,
                status=wiz.listing.status.value,
                stage=wiz.stage.value,
            )
        return wiz, results

    # ---------------------------- lifecycle ----------------------------- #
    def deprecate(
        self,
        key: str,
        *,
        replacement_key: str | None = None,
        migration_note: str = "",
        sunset_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LifecycleEvent:
        """Deprecate a listing (delegates to the lifecycle manager)."""
        event = self._lifecycle.deprecate(
            key,
            replacement_key=replacement_key,
            migration_note=migration_note,
            sunset_at=sunset_at,
            now=now,
        )
        logger.info("marketplace.deprecated", key=key, replacement=event.replacement_key)
        return event

    def sunset(
        self,
        key: str,
        *,
        replacement_key: str | None = None,
        migration_note: str = "",
        sunset_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LifecycleEvent:
        """Sunset a listing (delegates to the lifecycle manager)."""
        return self._lifecycle.sunset(
            key,
            replacement_key=replacement_key,
            migration_note=migration_note,
            sunset_at=sunset_at,
            now=now,
        )

    def retire(self, key: str, *, now: datetime | None = None) -> LifecycleEvent:
        """Retire a listing (delegates to the lifecycle manager)."""
        return self._lifecycle.retire(key, now=now)


__all__ = ["MarketplaceService"]
