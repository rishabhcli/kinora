"""A deterministic seed catalog of real hosted video models.

The marketplace ships with a curated starter catalog so the read-only API has
content out of the box and tests have a stable, named fixture set. These mirror
the models Kinora actually targets (DashScope/Wan + MiniMax/Hailuo) plus a few
representative long-tail entries to exercise filtering and ranking.

The data here is **descriptive metadata only** — no API keys, no endpoints, and
nothing that triggers a provider call. Timestamps are fixed (not wall-clock) so
the freshness ranking signal and any ordering assertions stay deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.video.marketplace.catalog import ModelCatalog
from app.video.marketplace.listing import (
    ModelListing,
    PricingTier,
    RegionAvailability,
    ReputationPointer,
    SampleRef,
)
from app.video.marketplace.types import (
    Capability,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    PricingModel,
    Region,
)

#: A fixed epoch so seeded listings are byte-stable across runs.
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _wan_t2v_turbo() -> ModelListing:
    return ModelListing(
        key="dashscope/wan2.1-t2v-turbo",
        provider="dashscope",
        model_id="wan2.1-t2v-turbo",
        version="2.1.0",
        display_name="Wan 2.1 T2V Turbo",
        summary="Fast hosted text-to-video; Kinora's default committed-zone renderer.",
        modalities=(Modality.TEXT_TO_VIDEO,),
        capabilities=(
            Capability.FAST_TURBO,
            Capability.HD_720P,
            Capability.NEGATIVE_PROMPT,
            Capability.SEED_CONTROL,
        ),
        max_duration_s=5.0,
        max_resolution="720p",
        pricing=(
            PricingTier(name="turbo", model=PricingModel.PER_SECOND, unit_price_usd=0.04),
        ),
        region=RegionAvailability(regions=(Region.GLOBAL,)),
        license_class=LicenseClass.COMMERCIAL_OK,
        tos_accepted=True,
        maturity=Maturity.GA,
        status=ListingStatus.ACTIVE,
        tags=("default", "turbo", "t2v"),
        samples=(
            SampleRef(
                uri="https://cdn.kinora.local/samples/wan21-t2v-turbo.mp4",
                modality=Modality.TEXT_TO_VIDEO,
                caption="lantern-lit alley, slow dolly-in",
                duration_s=5.0,
                width=1280,
                height=720,
            ),
        ),
        reputation=ReputationPointer(source="kinora-eval", score=0.78, sample_size=420, as_of=_T0),
        created_at=_T0,
        updated_at=_T0,
    )


def _wan_i2v_turbo() -> ModelListing:
    return ModelListing(
        key="dashscope/wan2.1-i2v-turbo",
        provider="dashscope",
        model_id="wan2.1-i2v-turbo",
        version="2.1.0",
        display_name="Wan 2.1 I2V Turbo",
        summary="Fast image/reference-to-video; identity-stable keyframe conditioning.",
        modalities=(Modality.IMAGE_TO_VIDEO, Modality.REFERENCE_TO_VIDEO),
        capabilities=(
            Capability.FAST_TURBO,
            Capability.HD_720P,
            Capability.FIRST_LAST_FRAME,
            Capability.CHARACTER_CONSISTENCY,
            Capability.SEED_CONTROL,
        ),
        max_duration_s=5.0,
        max_resolution="720p",
        pricing=(
            PricingTier(name="turbo", model=PricingModel.PER_SECOND, unit_price_usd=0.05),
        ),
        region=RegionAvailability(regions=(Region.GLOBAL,)),
        license_class=LicenseClass.COMMERCIAL_OK,
        tos_accepted=True,
        maturity=Maturity.GA,
        status=ListingStatus.ACTIVE,
        tags=("default", "turbo", "i2v", "r2v"),
        reputation=ReputationPointer(source="kinora-eval", score=0.81, sample_size=512, as_of=_T0),
        created_at=_T0,
        updated_at=_T0,
    )


def _wan25_t2v_preview() -> ModelListing:
    return ModelListing(
        key="dashscope/wan2.5-t2v-preview",
        provider="dashscope",
        model_id="wan2.5-t2v-preview",
        version="2.5.0",
        display_name="Wan 2.5 T2V (Preview)",
        summary="Higher-quality text-to-video; quality-override tier, longer takes.",
        modalities=(Modality.TEXT_TO_VIDEO,),
        capabilities=(
            Capability.HD_1080P,
            Capability.LONG_DURATION,
            Capability.CAMERA_CONTROL,
            Capability.NEGATIVE_PROMPT,
            Capability.SEED_CONTROL,
        ),
        max_duration_s=10.0,
        max_resolution="1080p",
        pricing=(
            PricingTier(name="quality", model=PricingModel.PER_SECOND, unit_price_usd=0.12),
        ),
        region=RegionAvailability(regions=(Region.GLOBAL,)),
        license_class=LicenseClass.COMMERCIAL_OK,
        tos_accepted=True,
        maturity=Maturity.PREVIEW,
        status=ListingStatus.PREVIEW,
        tags=("quality", "preview", "t2v", "1080p"),
        reputation=ReputationPointer(source="kinora-eval", score=0.88, sample_size=96, as_of=_T0),
        created_at=_T0,
        updated_at=_T0,
    )


def _minimax_hailuo_fast() -> ModelListing:
    return ModelListing(
        key="minimax/MiniMax-Hailuo-2.3-Fast",
        provider="minimax",
        model_id="MiniMax-Hailuo-2.3-Fast",
        version="2.3.0",
        display_name="MiniMax Hailuo 2.3 Fast",
        summary="Cheapest hosted i2v within budget; single-clip up-to-15s takes.",
        modalities=(Modality.IMAGE_TO_VIDEO, Modality.TEXT_TO_VIDEO),
        capabilities=(
            Capability.FAST_TURBO,
            Capability.HD_720P,
            Capability.LONG_DURATION,
            Capability.FIRST_LAST_FRAME,
        ),
        max_duration_s=15.0,
        max_resolution="720p",
        pricing=(
            PricingTier(
                name="fast",
                model=PricingModel.PER_SECOND,
                unit_price_usd=0.018,
                max_billable_units=15.0,
            ),
        ),
        region=RegionAvailability(regions=(Region.GLOBAL,)),
        license_class=LicenseClass.COMMERCIAL_OK,
        tos_accepted=True,
        maturity=Maturity.GA,
        status=ListingStatus.ACTIVE,
        tags=("cheap", "i2v", "15s", "budget"),
        reputation=ReputationPointer(source="kinora-eval", score=0.74, sample_size=210, as_of=_T0),
        created_at=_T0,
        updated_at=_T0,
    )


def _wan22_t2v_plus_deprecated() -> ModelListing:
    # A real deprecation: wan2.2-t2v-plus "fails at render" per CLAUDE.md, so it
    # ships pre-deprecated with a migration pointer to the preview quality model.
    return ModelListing(
        key="dashscope/wan2.2-t2v-plus",
        provider="dashscope",
        model_id="wan2.2-t2v-plus",
        version="2.2.0",
        display_name="Wan 2.2 T2V Plus",
        summary="Deprecated: unreliable at render; migrate to wan2.5-t2v-preview.",
        modalities=(Modality.TEXT_TO_VIDEO,),
        capabilities=(Capability.HD_1080P, Capability.LONG_DURATION),
        max_duration_s=8.0,
        max_resolution="1080p",
        pricing=(
            PricingTier(name="plus", model=PricingModel.PER_SECOND, unit_price_usd=0.10),
        ),
        region=RegionAvailability(regions=(Region.GLOBAL,)),
        license_class=LicenseClass.COMMERCIAL_OK,
        tos_accepted=True,
        maturity=Maturity.BETA,
        status=ListingStatus.DEPRECATED,
        replacement_key="dashscope/wan2.5-t2v-preview",
        migration_note="Unreliable at render; prefer wan2.5-t2v-preview for quality takes.",
        tags=("deprecated", "t2v"),
        reputation=ReputationPointer(source="kinora-eval", score=0.55, sample_size=64, as_of=_T0),
        created_at=_T0,
        updated_at=_T0,
    )


def _research_only_experimental() -> ModelListing:
    return ModelListing(
        key="labx/openvid-r1",
        provider="labx",
        model_id="openvid-r1",
        version="0.1.0",
        display_name="OpenVid R1 (Research)",
        summary="Experimental open research t2v; non-commercial license, no SLA.",
        modalities=(Modality.TEXT_TO_VIDEO,),
        capabilities=(Capability.HD_720P, Capability.SEED_CONTROL),
        max_duration_s=4.0,
        max_resolution="720p",
        pricing=(
            PricingTier(name="research", model=PricingModel.PER_SECOND, unit_price_usd=0.0),
        ),
        region=RegionAvailability(regions=(Region.US, Region.EU)),
        license_class=LicenseClass.RESEARCH_ONLY,
        tos_accepted=True,
        maturity=Maturity.EXPERIMENTAL,
        status=ListingStatus.PREVIEW,
        tags=("research", "experimental", "free"),
        created_at=_T0,
        updated_at=_T0,
    )


def seed_listings() -> list[ModelListing]:
    """The curated starter listings (deterministic; safe to call repeatedly)."""
    return [
        _wan_t2v_turbo(),
        _wan_i2v_turbo(),
        _wan25_t2v_preview(),
        _minimax_hailuo_fast(),
        _wan22_t2v_plus_deprecated(),
        _research_only_experimental(),
    ]


def seed_catalog() -> ModelCatalog:
    """A fresh :class:`ModelCatalog` populated with the seed listings."""
    return ModelCatalog(seed_listings())


__all__ = ["seed_catalog", "seed_listings"]
