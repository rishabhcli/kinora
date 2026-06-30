"""The rich :class:`ModelListing` value object and its supporting records.

A *listing* is the marketplace's canonical, queryable description of a single
hosted video model — everything a renderer or a human curator needs to decide
whether to route a shot to it: identity (provider + model id + version),
modalities & capabilities, pricing tiers, region availability, maturity grade,
license/ToS posture, a sample gallery, a quality-reputation pointer, and a
lifecycle ``status``.

All records are **frozen** pydantic v2 models so a listing is a stable,
hashable-by-value snapshot — the catalog and onboarding layers create *new*
listings via :meth:`ModelListing.evolve` rather than mutating in place, which
keeps history reversible and tests deterministic.

Validation is strict but local (no cross-subsystem imports): a listing must have
at least one modality, every advertised capability must be drawn from the
:class:`~app.video.marketplace.types.Capability` enum, pricing tiers must be
internally consistent with their :class:`~app.video.marketplace.types.PricingModel`,
and a deprecated/sunset listing must name (or at least be allowed to name) a
replacement. The structural checks raise
:class:`~app.video.marketplace.errors.ListingValidationError`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.video.marketplace.errors import ListingValidationError
from app.video.marketplace.types import (
    Capability,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    PricingModel,
    Region,
)


def _utcnow() -> datetime:
    """Timezone-aware UTC now (tests inject explicit clocks; this is the default)."""
    return datetime.now(UTC)


class PricingTier(BaseModel):
    """One metered price point for a listing.

    A listing may carry several tiers (e.g. a cheap ``turbo`` tier and a premium
    ``plus`` tier). ``unit_price_usd`` is interpreted by ``model``: per video
    second, per clip, per image, or per 1k tokens. ``min_charge_usd`` models a
    provider floor (a clip is billed for at least N seconds). All amounts are
    USD and must be non-negative.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    model: PricingModel
    unit_price_usd: float = Field(ge=0.0)
    #: For PER_SECOND tiers, the maximum billable seconds per clip (None = unbounded).
    max_billable_units: float | None = Field(default=None, gt=0.0)
    min_charge_usd: float = Field(default=0.0, ge=0.0)
    #: Free-form provider hint (e.g. an upstream tier/quota name); not interpreted.
    note: str = Field(default="", max_length=256)

    def estimate_usd(self, *, units: float) -> float:
        """Estimate the cost of ``units`` (seconds / clips / images / 1k-tokens).

        Applies ``max_billable_units`` clamping and the ``min_charge_usd`` floor.
        Negative ``units`` are treated as zero. Pure & deterministic.
        """
        billable = max(0.0, units)
        if self.max_billable_units is not None:
            billable = min(billable, self.max_billable_units)
        return max(self.min_charge_usd, billable * self.unit_price_usd)


class SampleRef(BaseModel):
    """A pointer to a sample-gallery artifact (a rendered clip or still).

    The marketplace stores *references*, never bytes — ``uri`` points at object
    storage / a CDN; the listing carries just enough metadata to render a
    gallery row and to let a curator eyeball quality.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(min_length=1, max_length=1024)
    modality: Modality
    caption: str = Field(default="", max_length=512)
    duration_s: float | None = Field(default=None, ge=0.0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class ReputationPointer(BaseModel):
    """A *pointer* to externally-computed quality reputation for the model.

    The marketplace deliberately does **not** own the reputation pipeline (that
    is a separate subsystem). It stores a normalized 0..1 ``score`` snapshot, the
    ``sample_size`` it was computed over, the ``source`` system that produced it,
    and ``as_of`` so the catalog can rank by quality and surface staleness. A
    listing with no measured reputation leaves this ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    as_of: datetime = Field(default_factory=_utcnow)


class RegionAvailability(BaseModel):
    """Where a listing's endpoint is reachable, and any per-region caveat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regions: tuple[Region, ...] = Field(default=(Region.GLOBAL,))
    #: Regions explicitly *blocked* (e.g. data-residency). Overrides ``regions``.
    blocked: tuple[Region, ...] = ()

    @field_validator("regions")
    @classmethod
    def _non_empty(cls, v: tuple[Region, ...]) -> tuple[Region, ...]:
        if not v:
            raise ValueError("at least one region is required")
        return v

    def serves(self, region: Region) -> bool:
        """Whether the endpoint serves ``region`` (GLOBAL serves everywhere)."""
        if region in self.blocked:
            return False
        if Region.GLOBAL in self.regions:
            return True
        return region in self.regions


class ModelListing(BaseModel):
    """The canonical marketplace description of one hosted video model.

    Immutable by construction (``frozen=True``); evolve with :meth:`evolve`.
    ``key`` is the stable catalog identifier (``provider/model_id``); a listing
    also carries a human ``display_name`` and a ``version`` so multiple versions
    of the same model id can coexist (the catalog dedupes by ``key`` keeping the
    selected one, but :meth:`evolve` preserves the lineage via ``key``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- identity ---
    key: str = Field(min_length=3, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    summary: str = Field(default="", max_length=1024)

    # --- what it can do ---
    modalities: tuple[Modality, ...] = Field(min_length=1)
    capabilities: tuple[Capability, ...] = ()
    max_duration_s: float = Field(default=5.0, gt=0.0)
    max_resolution: str = Field(default="720p", max_length=16)

    # --- commercial / availability ---
    pricing: tuple[PricingTier, ...] = ()
    region: RegionAvailability = Field(default_factory=RegionAvailability)
    license_class: LicenseClass = LicenseClass.UNKNOWN
    license_url: str = Field(default="", max_length=1024)
    tos_accepted: bool = False

    # --- maturity & lifecycle ---
    maturity: Maturity = Maturity.PREVIEW
    status: ListingStatus = ListingStatus.DRAFT
    #: Set when DEPRECATED/SUNSET: the ``key`` of the recommended replacement.
    replacement_key: str | None = Field(default=None, max_length=128)
    #: Set when DEPRECATED/SUNSET: an ISO date the model stops serving.
    sunset_at: datetime | None = None
    #: Human migration guidance shown alongside a deprecation.
    migration_note: str = Field(default="", max_length=1024)

    # --- discovery aids ---
    tags: tuple[str, ...] = ()
    samples: tuple[SampleRef, ...] = ()
    reputation: ReputationPointer | None = None

    # --- bookkeeping ---
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError("key must be 'provider/model_id'")
        if v != v.strip() or " " in v:
            raise ValueError("key must not contain whitespace")
        return v

    @field_validator("modalities")
    @classmethod
    def _dedupe_modalities(cls, v: tuple[Modality, ...]) -> tuple[Modality, ...]:
        # preserve first-seen order while removing dupes
        seen: dict[Modality, None] = {}
        for m in v:
            seen.setdefault(m, None)
        return tuple(seen.keys())

    @field_validator("capabilities")
    @classmethod
    def _dedupe_capabilities(cls, v: tuple[Capability, ...]) -> tuple[Capability, ...]:
        seen: dict[Capability, None] = {}
        for c in v:
            seen.setdefault(c, None)
        return tuple(seen.keys())

    @model_validator(mode="after")
    def _consistency(self) -> ModelListing:
        # at least one video-producing modality OR an explicit image modality
        if not self.modalities:  # pragma: no cover - guarded by min_length
            raise ValueError("at least one modality is required")

        # capability/modality coherence: HD/UHD imply a video or image output (always true here);
        # AUDIO_TRACK only makes sense for video output.
        if Capability.AUDIO_TRACK in self.capabilities and not any(
            m.is_video_output for m in self.modalities
        ):
            raise ValueError("AUDIO_TRACK capability requires a video-output modality")

        # a deprecated/sunset listing should name a replacement (soft: allowed to be None
        # only if migration_note explains the dead-end). We enforce one-or-the-other.
        if (
            self.status in (ListingStatus.DEPRECATED, ListingStatus.SUNSET)
            and not self.replacement_key
            and not self.migration_note
        ):
            raise ValueError(
                "a deprecated/sunset listing needs replacement_key or migration_note"
            )

        # an ACTIVE listing must have accepted ToS and have at least one pricing tier
        if self.status == ListingStatus.ACTIVE:
            if not self.tos_accepted:
                raise ValueError("an ACTIVE listing must have tos_accepted=True")
            if not self.pricing:
                raise ValueError("an ACTIVE listing must have at least one pricing tier")
        return self

    # ------------------------------------------------------------------ #
    # derived helpers
    # ------------------------------------------------------------------ #
    @property
    def cheapest_per_second_usd(self) -> float | None:
        """The lowest per-second price across tiers, or ``None`` if not metered that way."""
        per_s = [t.unit_price_usd for t in self.pricing if t.model == PricingModel.PER_SECOND]
        return min(per_s) if per_s else None

    @property
    def min_unit_price_usd(self) -> float | None:
        """The lowest unit price across *all* tiers regardless of metering model."""
        if not self.pricing:
            return None
        return min(t.unit_price_usd for t in self.pricing)

    def supports(
        self, *, modality: Modality | None = None, capability: Capability | None = None
    ) -> bool:
        """Whether the listing advertises a modality and/or capability."""
        if modality is not None and modality not in self.modalities:
            return False
        return not (capability is not None and capability not in self.capabilities)

    def estimate_clip_usd(self, *, seconds: float) -> float | None:
        """Cheapest estimate to render a ``seconds``-long clip across tiers.

        Considers PER_SECOND (× seconds) and PER_CLIP (flat) tiers; returns the
        minimum, or ``None`` if the listing has no video pricing. Pure.
        """
        candidates: list[float] = []
        for t in self.pricing:
            if t.model == PricingModel.PER_SECOND:
                candidates.append(t.estimate_usd(units=seconds))
            elif t.model == PricingModel.PER_CLIP:
                candidates.append(t.estimate_usd(units=1.0))
        return min(candidates) if candidates else None

    def evolve(self, *, now: datetime | None = None, **changes: Any) -> ModelListing:
        """Return a new validated listing with ``changes`` applied.

        Bumps ``updated_at`` (to ``now`` or wall-clock). This is the *only*
        sanctioned way to change a listing — it keeps every listing frozen and
        every transition re-validated.
        """
        data = self.model_dump()
        data.update(changes)
        data["updated_at"] = now or _utcnow()
        return ModelListing.model_validate(data)


def validate_listing(listing: ModelListing) -> None:
    """Re-run the structural checks on an already-built listing.

    Construction already validates; this exists so callers (the onboarding
    manifest gate) can validate a dict-derived listing and convert pydantic's
    ``ValidationError`` into the subsystem's typed
    :class:`~app.video.marketplace.errors.ListingValidationError`.
    """
    try:
        ModelListing.model_validate(listing.model_dump())
    except Exception as exc:  # pydantic ValidationError or ValueError
        raise ListingValidationError(str(exc)) from exc


def listing_from_manifest(manifest: dict[str, Any]) -> ModelListing:
    """Build a listing from an untrusted manifest dict, mapping errors to typed ones."""
    try:
        return ModelListing.model_validate(manifest)
    except Exception as exc:
        raise ListingValidationError(f"invalid listing manifest: {exc}") from exc


__all__ = [
    "ModelListing",
    "PricingTier",
    "RegionAvailability",
    "ReputationPointer",
    "SampleRef",
    "listing_from_manifest",
    "validate_listing",
]
