"""Read-only catalog API for the video-provider marketplace.

Mounted under ``/video/marketplace``. The surface is deliberately **read-only**
— browsing, searching, comparing, and inspecting migration hints — because the
write paths (onboarding gates, lifecycle transitions) are privileged operations
that belong to a curator workflow, not an open API. The router maps the
subsystem's typed errors to HTTP status codes and translates the rich domain
models to JSON via thin response schemas.

The service is a process-level singleton built from the deterministic seed
catalog (no DB / network / container dependency), so this router can be mounted
and TestClient-driven in complete isolation. Routes:

* ``GET  /video/marketplace/listings`` — list (optionally include retired).
* ``GET  /video/marketplace/listings/{key:path}`` — one listing.
* ``POST /video/marketplace/search`` — filter + rank (returns scored results).
* ``GET  /video/marketplace/compare?left=&right=`` — side-by-side of two models.
* ``GET  /video/marketplace/listings/{key:path}/migration`` — migration hint.
* ``GET  /video/marketplace/capabilities`` — the capability/modality vocabulary.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import APIError
from app.video.marketplace.catalog import CatalogQuery
from app.video.marketplace.errors import (
    ListingNotFoundError,
    ListingValidationError,
    MarketplaceError,
)
from app.video.marketplace.listing import ModelListing
from app.video.marketplace.service import MarketplaceService
from app.video.marketplace.types import (
    Capability,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    Region,
)

router = APIRouter(prefix="/video/marketplace", tags=["video-marketplace"])

# Process-level singleton seeded catalog. The marketplace read model is in-memory
# and deterministic; the API never mutates it, so a single instance is safe.
_SERVICE: MarketplaceService | None = None


def get_service() -> MarketplaceService:
    """Lazily build (once) and return the shared marketplace service."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MarketplaceService()
    return _SERVICE


def _map_error(exc: MarketplaceError) -> APIError:
    if isinstance(exc, ListingNotFoundError):
        return APIError(exc.code, exc.message, status=404)
    if isinstance(exc, ListingValidationError):
        return APIError(exc.code, exc.message, status=422)
    return APIError(exc.code, exc.message, status=400)


# --------------------------------------------------------------------------- #
# Response schemas (thin views over the domain models)
# --------------------------------------------------------------------------- #
class ListingView(BaseModel):
    """A JSON-friendly projection of a :class:`ModelListing`."""

    model_config = ConfigDict(extra="forbid")

    key: str
    provider: str
    model_id: str
    version: str
    display_name: str
    summary: str
    modalities: list[str]
    capabilities: list[str]
    max_duration_s: float
    max_resolution: str
    pricing: list[dict[str, Any]]
    regions: list[str]
    license_class: str
    maturity: str
    status: str
    replacement_key: str | None
    tags: list[str]
    sample_count: int
    reputation_score: float | None
    cheapest_per_second_usd: float | None

    @classmethod
    def of(cls, li: ModelListing) -> ListingView:
        return cls(
            key=li.key,
            provider=li.provider,
            model_id=li.model_id,
            version=li.version,
            display_name=li.display_name,
            summary=li.summary,
            modalities=[m.value for m in li.modalities],
            capabilities=[c.value for c in li.capabilities],
            max_duration_s=li.max_duration_s,
            max_resolution=li.max_resolution,
            pricing=[t.model_dump(mode="json") for t in li.pricing],
            regions=[r.value for r in li.region.regions],
            license_class=li.license_class.value,
            maturity=li.maturity.value,
            status=li.status.value,
            replacement_key=li.replacement_key,
            tags=list(li.tags),
            sample_count=len(li.samples),
            reputation_score=li.reputation.score if li.reputation else None,
            cheapest_per_second_usd=li.cheapest_per_second_usd,
        )


class ScoredView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing: ListingView
    score: float
    breakdown: dict[str, float]


class SearchBody(BaseModel):
    """Request body for ``POST /search`` (mirrors :class:`CatalogQuery`)."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    modality: Modality | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    region: Region | None = None
    max_price_per_second_usd: float | None = Field(default=None, ge=0.0)
    min_maturity: Maturity | None = None
    license_class: LicenseClass | None = None
    statuses: list[ListingStatus] = Field(default_factory=list)
    include_retired: bool = False
    min_duration_s: float | None = Field(default=None, gt=0.0)
    limit: int = Field(default=50, ge=1, le=500)

    def to_query(self) -> CatalogQuery:
        return CatalogQuery(
            text=self.text,
            modality=self.modality,
            capabilities=tuple(self.capabilities),
            region=self.region,
            max_price_per_second_usd=self.max_price_per_second_usd,
            min_maturity=self.min_maturity,
            license_class=self.license_class,
            statuses=tuple(self.statuses),
            include_retired=self.include_retired,
            min_duration_s=self.min_duration_s,
            limit=self.limit,
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/listings", response_model=list[ListingView])
async def list_listings(
    include_retired: Annotated[bool, Query()] = False,
) -> list[ListingView]:
    """List all default-visible listings (optionally include retired)."""
    svc = get_service()
    return [ListingView.of(li) for li in svc.list_listings(include_retired=include_retired)]


@router.post("/search", response_model=list[ScoredView])
async def search(body: SearchBody) -> list[ScoredView]:
    """Filter + rank the catalog; returns scored results, best first."""
    svc = get_service()
    scored = svc.search(body.to_query())
    return [
        ScoredView(listing=ListingView.of(s.listing), score=s.score, breakdown=s.breakdown)
        for s in scored
    ]


@router.get("/compare")
async def compare(
    left: Annotated[str, Query(min_length=3)],
    right: Annotated[str, Query(min_length=3)],
) -> dict[str, Any]:
    """Structured side-by-side comparison of two listings."""
    svc = get_service()
    try:
        comparison = svc.compare(left, right)
    except MarketplaceError as exc:
        raise _map_error(exc) from exc
    return comparison.model_dump(mode="json")


@router.get("/capabilities")
async def capabilities() -> dict[str, list[str]]:
    """The marketplace vocabulary (modalities, capabilities, maturities, …)."""
    return {
        "modalities": [m.value for m in Modality],
        "capabilities": [c.value for c in Capability],
        "maturities": [m.value for m in Maturity],
        "statuses": [s.value for s in ListingStatus],
        "regions": [r.value for r in Region],
        "license_classes": [lc.value for lc in LicenseClass],
    }


# NOTE: the {key:path} routes are declared last so the static paths above
# (``/search``, ``/compare``, ``/capabilities``) are matched first; keys contain
# a ``/`` (``provider/model_id``) so they need the ``:path`` converter.
@router.get("/listings/{key:path}/migration")
async def migration_hint(key: str) -> dict[str, Any]:
    """Migration guidance for a (typically deprecated) listing."""
    svc = get_service()
    try:
        hint = svc.migration_hint(key)
    except MarketplaceError as exc:
        raise _map_error(exc) from exc
    return {
        "from_key": hint.from_key,
        "to_key": hint.to_key,
        "note": hint.note,
        "sunset_at": hint.sunset_at.isoformat() if hint.sunset_at else None,
        "lost_capabilities": [c.value for c in hint.lost_capabilities],
        "gained_capabilities": [c.value for c in hint.gained_capabilities],
        "price_delta_per_second_usd": hint.price_delta_per_second_usd,
        "replacement_available": hint.replacement_available,
    }


@router.get("/listings/{key:path}", response_model=ListingView)
async def get_listing(key: str) -> ListingView:
    """Fetch a single listing by ``provider/model_id`` key."""
    svc = get_service()
    try:
        return ListingView.of(svc.get(key))
    except MarketplaceError as exc:
        raise _map_error(exc) from exc


__all__ = ["router", "get_service"]
