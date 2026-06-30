"""Provider identity-conditioning capability profiles (the local seam I own).

Kinora's whole thesis is *visual consistency across a long adaptation*: a
character or setting must look the same no matter which video model renders the
shot (kinora.md §0 "consistency is memory", §9.5 CCS). But providers accept the
locked identity in wildly different shapes:

* **reference image set** — Wan ``reference_to_video`` takes several locked
  appearance refs (``reference_image_urls``);
* **single first frame** — image-to-video / first-last-frame takes *one* driving
  image (``image_url`` / ``first_frame_url``);
* **inline base64** — some providers want the bytes inline, not a URL;
* **character / IP-Adapter id** — some carry a registered subject token instead
  of pixels;
* **nothing** — a pure text-to-video model accepts no identity input at all.

This module defines a small, *local* :class:`CapabilityProfile` describing what a
given video backend can ingest. It deliberately does **not** import the provider
classes (no coupling to ``app.providers.video`` / ``minimax``): a profile is a
plain, declarative capability record that the
:class:`~app.video.identity.conditioner.IdentityConditioner` reads to choose the
best conditioning strategy and emit provider-appropriate fields. Profiles for the
repo's real backends live in :data:`KNOWN_PROFILES`; an unknown backend resolves
to :data:`DEFAULT_PROFILE` (assume only a single first frame — the universal
lowest common denominator that the keyframe fallback can always satisfy).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

# --------------------------------------------------------------------------- #
# Conditioning kinds
# --------------------------------------------------------------------------- #


class ConditioningKind(StrEnum):
    """A single way a provider can ingest a locked identity.

    A provider declares the *set* it supports; the conditioner picks the single
    best one for a shot. Ordered loosely by identity fidelity (how strongly the
    locked appearance survives into the clip) in :data:`FIDELITY_RANK`.
    """

    #: A set of locked appearance reference images (Wan r2v ``reference_image_urls``).
    #: The strongest conditioning — multiple poses pin the subject directly.
    REFERENCE_SET = "reference_set"
    #: A registered subject/IP-Adapter/character id carried as an opaque token.
    CHARACTER_ID = "character_id"
    #: Inline base64-encoded reference image bytes (no object-store round-trip).
    INLINE_IMAGE = "inline_image"
    #: A single driving / start frame (image-to-video, ``image_url``).
    FIRST_FRAME = "first_frame"
    #: A first + last frame composition (first-last-frame interpolation).
    FIRST_LAST_FRAME = "first_last_frame"
    #: No identity input accepted (pure text-to-video). The conditioner must fall
    #: back to baking identity into a keyframe and driving a *different* mode.
    NONE = "none"


#: Higher = stronger identity preservation. Used to break ties when several
#: supported kinds are viable for a shot.
FIDELITY_RANK: dict[ConditioningKind, int] = {
    ConditioningKind.REFERENCE_SET: 5,
    ConditioningKind.CHARACTER_ID: 4,
    ConditioningKind.INLINE_IMAGE: 3,
    ConditioningKind.FIRST_LAST_FRAME: 2,
    ConditioningKind.FIRST_FRAME: 1,
    ConditioningKind.NONE: 0,
}


class ImageTransport(IntEnum):
    """How a provider wants reference image *pixels* delivered."""

    #: A (presigned) https / oss URL the provider fetches itself.
    URL = 0
    #: A ``data:`` URI carrying inline base64 bytes.
    DATA_URI = 1
    #: Raw base64 (no ``data:`` scheme prefix) in a dedicated field.
    BASE64 = 2


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """What one video backend can ingest for identity conditioning (declarative).

    This is the *local* capability seam owned by ``app/video/identity`` — it is
    intentionally decoupled from the concrete provider transports so the
    conditioner can reason about any present or future backend uniformly.

    Attributes:
        name: Stable backend identifier (matches the router's ``backend.name``).
        supported: The conditioning kinds this backend accepts, as a set.
        max_reference_images: Cap on refs for :data:`ConditioningKind.REFERENCE_SET`
            (a provider may accept e.g. at most 3 locked poses).
        image_transport: How reference pixels must be delivered.
        accepts_negative_prompt: Whether a negative prompt steers identity (used by
            the conditioner to inject anti-drift tokens, e.g. "warped face").
        accepts_seed: Whether a fixed seed is honoured (determinism aids
            consistency across re-renders).
        accepts_reference_voice: Whether a cloned-voice reference URL rides along
            (r2v carries it; most do not).
        notes: Free-form provenance for telemetry/debugging.
    """

    name: str
    supported: frozenset[ConditioningKind]
    max_reference_images: int = 0
    image_transport: ImageTransport = ImageTransport.URL
    accepts_negative_prompt: bool = True
    accepts_seed: bool = True
    accepts_reference_voice: bool = False
    notes: str = ""

    def supports(self, kind: ConditioningKind) -> bool:
        """True when ``kind`` is in :attr:`supported`."""
        return kind in self.supported

    @property
    def has_direct_reference(self) -> bool:
        """True when the backend can pin identity *without* the keyframe fallback.

        A backend with only :data:`ConditioningKind.NONE` (or nothing usable) has
        no direct reference path and must rely on a baked keyframe + i2v drive.
        """
        return any(
            self.supports(k)
            for k in (
                ConditioningKind.REFERENCE_SET,
                ConditioningKind.CHARACTER_ID,
                ConditioningKind.INLINE_IMAGE,
                ConditioningKind.FIRST_FRAME,
                ConditioningKind.FIRST_LAST_FRAME,
            )
        )

    def best_supported(self) -> ConditioningKind:
        """The highest-fidelity conditioning kind this backend supports."""
        if not self.supported:
            return ConditioningKind.NONE
        return max(self.supported, key=lambda k: FIDELITY_RANK[k])


# --------------------------------------------------------------------------- #
# Known backend profiles (the repo's real video backends + a safe default)
# --------------------------------------------------------------------------- #

#: A registry of capability profiles keyed by a *family* token. Real router
#: backend names (e.g. ``video:wan2.1-i2v-turbo``) are matched by prefix in
#: :func:`profile_for`, so a turbo / quality id of the same family shares one
#: profile. Empty body of the parent package keeps this the single source of
#: truth for "what can each model ingest".
KNOWN_PROFILES: dict[str, CapabilityProfile] = {
    # Wan reference-to-video families: the strongest path — a locked appearance
    # ref set plus a cloned-voice ref. r2v ids in this repo (``*-i2v-*`` carry the
    # r2v reference set in the media protocol).
    "wan-r2v": CapabilityProfile(
        name="wan-r2v",
        supported=frozenset(
            {
                ConditioningKind.REFERENCE_SET,
                ConditioningKind.FIRST_FRAME,
                ConditioningKind.FIRST_LAST_FRAME,
            }
        ),
        max_reference_images=3,
        image_transport=ImageTransport.URL,
        accepts_reference_voice=True,
        notes="Wan reference-to-video: locked appearance refs + cloned voice.",
    ),
    # Wan image-to-video families: a single driving frame only (no ref set).
    "wan-i2v": CapabilityProfile(
        name="wan-i2v",
        supported=frozenset(
            {ConditioningKind.FIRST_FRAME, ConditioningKind.FIRST_LAST_FRAME}
        ),
        max_reference_images=1,
        image_transport=ImageTransport.URL,
        notes="Wan image-to-video: single first frame / first-last-frame.",
    ),
    # Wan text-to-video families: NOTHING. Identity must be baked into a keyframe
    # and the shot re-routed to i2v (the keyframe fallback).
    "wan-t2v": CapabilityProfile(
        name="wan-t2v",
        supported=frozenset({ConditioningKind.NONE}),
        notes="Wan text-to-video: no identity input — keyframe fallback required.",
    ),
    # MiniMax (Hailuo): a single first frame, delivered inline as base64.
    "minimax": CapabilityProfile(
        name="minimax",
        supported=frozenset({ConditioningKind.FIRST_FRAME, ConditioningKind.INLINE_IMAGE}),
        max_reference_images=1,
        image_transport=ImageTransport.BASE64,
        notes="MiniMax Hailuo: single first frame, inline base64.",
    ),
}

#: The universal lowest common denominator: assume a backend takes a single first
#: frame. The keyframe fallback can always synthesise one, so an unknown backend
#: never blocks identity conditioning entirely.
DEFAULT_PROFILE: CapabilityProfile = CapabilityProfile(
    name="default",
    supported=frozenset({ConditioningKind.FIRST_FRAME}),
    max_reference_images=1,
    image_transport=ImageTransport.URL,
    notes="Unknown backend — assume single first frame (keyframe-fallback safe).",
)


def profile_for(backend_name: str) -> CapabilityProfile:
    """Resolve the capability profile for a backend name (substring match, pure).

    The router names a backend after its model id (e.g.
    ``video:wan2.1-i2v-turbo`` or ``video:wan2.7-i2v-...``). We classify by token:

    * ``*r2v*`` / ``*reference*``  → :data:`KNOWN_PROFILES["wan-r2v"]`
    * ``*i2v*`` / ``*image*``      → :data:`KNOWN_PROFILES["wan-i2v"]`
    * ``*t2v*`` / ``*text*``       → :data:`KNOWN_PROFILES["wan-t2v"]`
    * ``*minimax*`` / ``*hailuo*`` → :data:`KNOWN_PROFILES["minimax"]`
    * otherwise                    → :data:`DEFAULT_PROFILE`.

    Note that in this repo the r2v *id* is currently an i2v id; the conditioner
    treats the **render mode**, not the model id, as authoritative for which kind
    to emit (see :class:`IdentityConditioner`). This resolver only describes the
    backend's *ceiling* of capability.
    """
    low = backend_name.lower()
    if "minimax" in low or "hailuo" in low:
        return KNOWN_PROFILES["minimax"]
    if "r2v" in low or "reference" in low:
        return KNOWN_PROFILES["wan-r2v"]
    if "t2v" in low or "text_to" in low or "text-to" in low:
        return KNOWN_PROFILES["wan-t2v"]
    if "i2v" in low or "image_to" in low or "image-to" in low:
        return KNOWN_PROFILES["wan-i2v"]
    return DEFAULT_PROFILE


__all__ = [
    "DEFAULT_PROFILE",
    "FIDELITY_RANK",
    "KNOWN_PROFILES",
    "CapabilityProfile",
    "ConditioningKind",
    "ImageTransport",
    "profile_for",
]
