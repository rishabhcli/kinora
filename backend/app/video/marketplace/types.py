"""Local, self-owned vocabulary for the video-provider marketplace.

This subsystem is **additive and self-contained** (Round-3 FINAL constraint:
Rounds 1 & 2 are *not* merged, so their provider-registry / capability packages
cannot be imported). Every enum and value type the marketplace reasons over is
therefore defined *here* and owned by ``app.video.marketplace`` — nothing in this
file depends on any other Kinora subsystem.

The vocabulary is deliberately rich enough to describe the real hosted-video
landscape Kinora targets (DashScope/Wan, MiniMax/Hailuo, and the long tail of
text-to-video / image-to-video / reference-to-video models) without committing
to any one provider's wire format:

* :class:`Modality` — what a model consumes/produces (text->video, image->video,
  reference->video, video->video, text->image, audio).
* :class:`Capability` — finer-grained features a model advertises (duration
  ceilings, resolution tiers, audio track, camera control, …).
* :class:`Maturity` — a release-stability grade (experimental → ga), with a
  monotonic ordinal so the catalog can rank "more proven" models higher.
* :class:`ListingStatus` — the marketplace lifecycle state of a listing.
* :class:`Region` — a coarse availability geography.
* :class:`LicenseClass` / :class:`PricingModel` — commercial-usage posture.

All enums are ``str``-valued so they serialize cleanly through pydantic v2 / JSON
and are stable to compare in tests.
"""

from __future__ import annotations

from enum import StrEnum


class Modality(StrEnum):
    """A high-level input→output transform a video model performs."""

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    VIDEO_TO_VIDEO = "video_to_video"
    TEXT_TO_IMAGE = "text_to_image"
    KEYFRAME_INTERPOLATION = "keyframe_interpolation"

    @property
    def is_video_output(self) -> bool:
        """Whether this modality *produces* a video clip (vs. an image)."""
        return self is not Modality.TEXT_TO_IMAGE


class Capability(StrEnum):
    """A discrete, advertisable feature of a model.

    These compose orthogonally with :class:`Modality`: a single text-to-video
    model may advertise ``LONG_DURATION``, ``HD_1080P`` and ``CAMERA_CONTROL``.
    The catalog filters and the onboarding conformance gate both reason over
    this set, so it is intentionally explicit rather than free-form strings.
    """

    LONG_DURATION = "long_duration"  # clips beyond the common 5s ceiling
    HD_720P = "hd_720p"
    HD_1080P = "hd_1080p"
    UHD_4K = "uhd_4k"
    AUDIO_TRACK = "audio_track"  # emits a synced audio/voice track
    CAMERA_CONTROL = "camera_control"  # pan/zoom/orbit prompt directives
    MOTION_BRUSH = "motion_brush"  # region-targeted motion control
    STYLE_TRANSFER = "style_transfer"
    CHARACTER_CONSISTENCY = "character_consistency"  # identity-stable across shots
    FIRST_LAST_FRAME = "first_last_frame"  # start+end keyframe conditioning
    NEGATIVE_PROMPT = "negative_prompt"
    SEED_CONTROL = "seed_control"  # deterministic reproducibility
    FAST_TURBO = "fast_turbo"  # latency-optimized "turbo" tier
    LIP_SYNC = "lip_sync"  # mouth-to-audio alignment


class Maturity(StrEnum):
    """Release-stability grade. Ordered ``experimental < preview < beta < ga``."""

    EXPERIMENTAL = "experimental"
    PREVIEW = "preview"
    BETA = "beta"
    GA = "ga"

    @property
    def grade(self) -> int:
        """A monotonic 0..3 ordinal (higher = more proven). Used in ranking."""
        return _MATURITY_ORDER[self]


_MATURITY_ORDER: dict[Maturity, int] = {
    Maturity.EXPERIMENTAL: 0,
    Maturity.PREVIEW: 1,
    Maturity.BETA: 2,
    Maturity.GA: 3,
}


class ListingStatus(StrEnum):
    """The marketplace lifecycle state of a :class:`~.listing.ModelListing`.

    The onboarding wizard drives ``DRAFT → STAGED(PREVIEW) → ACTIVE``; the
    lifecycle manager drives ``ACTIVE → DEPRECATED → SUNSET → RETIRED``. Only
    ``ACTIVE`` and (optionally) ``DEPRECATED`` listings are eligible for new
    render assignments; ``RETIRED`` listings are hidden from the default catalog.
    """

    DRAFT = "draft"
    PREVIEW = "preview"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"
    RETIRED = "retired"

    @property
    def is_selectable(self) -> bool:
        """Whether a listing in this status may be picked for *new* renders."""
        return self in (ListingStatus.ACTIVE, ListingStatus.PREVIEW)

    @property
    def is_visible_by_default(self) -> bool:
        """Whether the catalog shows this status without an explicit opt-in."""
        return self is not ListingStatus.RETIRED


class Region(StrEnum):
    """A coarse availability geography for a model endpoint."""

    GLOBAL = "global"
    US = "us"
    EU = "eu"
    APAC = "apac"
    CN = "cn"


class LicenseClass(StrEnum):
    """Commercial-usage posture of a model's license / ToS."""

    COMMERCIAL_OK = "commercial_ok"  # cleared for paid commercial output
    RESEARCH_ONLY = "research_only"  # non-commercial / evaluation only
    RESTRICTED = "restricted"  # commercial allowed with named restrictions
    UNKNOWN = "unknown"

    @property
    def commercial_safe(self) -> bool:
        """Whether output may be used commercially without further review."""
        return self is LicenseClass.COMMERCIAL_OK


class PricingModel(StrEnum):
    """How a pricing tier meters cost."""

    PER_SECOND = "per_second"  # USD per generated video-second
    PER_CLIP = "per_clip"  # flat USD per generated clip
    PER_IMAGE = "per_image"  # USD per generated image (t2i models)
    PER_TOKEN = "per_token"  # USD per 1k prompt tokens (rare, multimodal)


__all__ = [
    "Capability",
    "LicenseClass",
    "ListingStatus",
    "Maturity",
    "Modality",
    "PricingModel",
    "Region",
]
