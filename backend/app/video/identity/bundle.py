"""The canonical :class:`IdentityBundle` — one entity's locked visual truth.

A bundle is the *normalized* identity of one canon entity (character, location,
prop, or style) assembled from the canon graph (kinora.md §8.1): the locked
reference image set (front / three-quarter / profile poses, paid for once and
amortised across the whole book, §6 "Lock identity"), the appearance descriptor
(the 1152-d CLIP-style embedding the Critic compares against, §9.5), an optional
registered subject/IP-Adapter id, and a *style centroid* — the mean of the locked
refs' descriptors, which is the cheap rotation-invariant signature the self-check
hook scores output crops against.

The bundle is provider-agnostic on purpose. It carries *everything* any provider
might need; the :class:`~app.video.identity.conditioner.IdentityConditioner`
projects it down to whatever a given backend can ingest. Bytes-or-URL is modelled
explicitly so the conditioner can satisfy URL, ``data:`` URI, and inline-base64
transports from the same locked ref.
"""

from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum

# --------------------------------------------------------------------------- #
# Pose / framing taxonomy
# --------------------------------------------------------------------------- #


class Pose(StrEnum):
    """A canonical framing/pose for a locked reference image.

    Matches the canon graph's ``reference_images[].pose`` tokens (§8.1). ``UNKNOWN``
    is the safe default for a ref whose pose was never annotated.
    """

    FRONT = "front"
    THREE_QUARTER = "3q"
    PROFILE = "profile"
    BACK = "back"
    FULL_BODY = "full_body"
    CLOSEUP = "closeup"
    ESTABLISHING = "establishing"  # locations
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: str | None) -> Pose:
        """Map a raw canon pose token to a :class:`Pose` (tolerant of aliases)."""
        if not value:
            return cls.UNKNOWN
        token = value.strip().lower().replace("-", "_")
        aliases = {
            "three_quarter": cls.THREE_QUARTER,
            "threequarter": cls.THREE_QUARTER,
            "34": cls.THREE_QUARTER,
            "side": cls.PROFILE,
            "rear": cls.BACK,
            "wide": cls.ESTABLISHING,
            "close_up": cls.CLOSEUP,
            "close": cls.CLOSEUP,
            "full": cls.FULL_BODY,
        }
        if token in aliases:
            return aliases[token]
        try:
            return cls(token)
        except ValueError:
            return cls.UNKNOWN


# --------------------------------------------------------------------------- #
# A locked reference image
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LockedReference:
    """One locked reference image for an entity, with its appearance descriptor.

    Exactly one of :attr:`url` / :attr:`image_bytes` should be populated at
    construction (a canon ref usually arrives as a presigned URL; a freshly baked
    keyframe arrives as bytes). The conditioner derives whichever transport a
    backend needs; :meth:`as_data_uri` / :meth:`as_base64` require bytes.

    Attributes:
        ref_id: The canon reference key (e.g. ``char_elsa_001@v3:front``).
        pose: The framing this ref depicts.
        url: A (presigned) fetch URL, when the ref lives in object storage.
        image_bytes: Inline pixels, when carried directly (e.g. a baked keyframe).
        descriptor: The 1152-d appearance embedding of *this* ref (unit-norm).
        locked: Whether this ref is canon-locked (vs a speculative/derived frame).
        quality: 0..1 intrinsic quality of the ref (sharpness/clean-bg); ranking
            tie-breaker so a crisp front beats a noisy one of the same pose.
        mime: Image MIME type for ``data:`` URIs (defaults to PNG).
    """

    ref_id: str
    pose: Pose = Pose.UNKNOWN
    url: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    descriptor: tuple[float, ...] = field(default=(), repr=False)
    locked: bool = True
    quality: float = 1.0
    mime: str = "image/png"

    def __post_init__(self) -> None:
        if self.url is None and self.image_bytes is None:
            raise ValueError(f"LockedReference {self.ref_id!r} has neither url nor bytes")
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError(f"quality must be in [0,1], got {self.quality}")

    @property
    def has_bytes(self) -> bool:
        return self.image_bytes is not None

    @property
    def has_descriptor(self) -> bool:
        return len(self.descriptor) > 0

    def as_base64(self) -> str:
        """Raw base64 of the bytes (no scheme prefix). Requires :attr:`image_bytes`."""
        if self.image_bytes is None:
            raise ValueError(f"LockedReference {self.ref_id!r} has no bytes for base64")
        return base64.b64encode(self.image_bytes).decode("ascii")

    def as_data_uri(self) -> str:
        """A ``data:`` URI of the bytes. Requires :attr:`image_bytes`."""
        return f"data:{self.mime};base64,{self.as_base64()}"

    def transport_value(self, *, inline: bool, data_uri: bool) -> str | None:
        """The pixel value for the requested transport, or ``None`` if unavailable.

        ``inline`` selects bytes-based transports; ``data_uri`` chooses the
        ``data:`` scheme over raw base64. A URL-only ref returns its URL when
        ``inline`` is False, else ``None`` (the caller must materialise bytes).
        """
        if not inline:
            return self.url
        if self.image_bytes is None:
            return None
        return self.as_data_uri() if data_uri else self.as_base64()


# --------------------------------------------------------------------------- #
# Vector helpers (local, dependency-free; mirror embeddings.cosine semantics)
# --------------------------------------------------------------------------- #


def cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    """Cosine similarity of two vectors (0.0 on a zero/empty/mismatched vector).

    Mirrors :func:`app.providers.embeddings.cosine` but tolerates empty/mismatched
    inputs by returning ``0.0`` instead of raising — the self-check must degrade
    to "max drift", never crash a render.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: list[tuple[float, ...]]) -> tuple[float, ...]:
    """The unit-normalized mean of equal-length vectors (``()`` when empty/ragged).

    This is the entity's *style centroid* — a single rotation/pose-averaged
    signature. Ragged or empty input yields ``()`` (no centroid) rather than an
    error, so a partially-embedded bundle is still constructible.
    """
    usable = [v for v in vectors if v]
    if not usable:
        return ()
    dim = len(usable[0])
    if any(len(v) != dim for v in usable):
        return ()
    sums = [math.fsum(v[i] for v in usable) for i in range(dim)]
    norm = math.sqrt(math.fsum(s * s for s in sums))
    if norm == 0.0:
        return tuple(0.0 for _ in range(dim))
    return tuple(s / norm for s in sums)


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IdentityBundle:
    """The normalized, provider-agnostic locked identity of one canon entity.

    Assembled once from a canon entity slice (§8.1) and reused for every shot the
    entity appears in. The conditioner reads it; nothing here knows about any
    provider's request shape.

    Attributes:
        entity_key: The canon entity key (e.g. ``char_elsa_001``).
        entity_type: ``character`` / ``location`` / ``prop`` / ``style``.
        name: Human name (for prompt injection and telemetry).
        version: The canon version this bundle was assembled at (drift is scoped
            to a version; a re-locked entity gets a fresh bundle).
        references: The locked reference images (ranked by the selection policy).
        appearance_descriptor: The entity's canonical appearance embedding (§8.1
            ``appearance.embedding``) — the primary self-check anchor. Falls back
            to the style centroid when the canon never stored one.
        character_id: An optional registered subject / IP-Adapter token.
        appearance_prompt: A short locked appearance phrase ("platinum braid,
            ice-blue gown") injected into prompts for text-only providers.
        negative_tokens: Anti-drift negatives ("warped face, extra fingers").
        voice_ref_url: Cloned-voice reference URL (carried for r2v).
    """

    entity_key: str
    entity_type: str
    name: str
    version: int = 1
    references: tuple[LockedReference, ...] = ()
    appearance_descriptor: tuple[float, ...] = field(default=(), repr=False)
    character_id: str | None = None
    appearance_prompt: str = ""
    negative_tokens: tuple[str, ...] = ()
    voice_ref_url: str | None = None

    # -- derived signatures ---------------------------------------------- #

    @property
    def locked_references(self) -> tuple[LockedReference, ...]:
        """Only the canon-locked refs (excludes derived/speculative frames)."""
        return tuple(r for r in self.references if r.locked)

    @property
    def style_centroid(self) -> tuple[float, ...]:
        """Pose-averaged descriptor across the locked refs (rotation-robust).

        The cheap, robust signature for the self-check: an output crop at an
        unusual angle still scores well against the centroid even when it would
        miss any single locked pose.
        """
        return centroid([r.descriptor for r in self.locked_references if r.has_descriptor])

    @property
    def anchor_descriptor(self) -> tuple[float, ...]:
        """The vector the self-check compares against: appearance, else centroid.

        Prefer the canon's stored appearance embedding (it is the global truth);
        if absent, the locked-ref centroid is the best available proxy.
        """
        return self.appearance_descriptor or self.style_centroid

    @property
    def has_references(self) -> bool:
        return bool(self.locked_references)

    @property
    def has_inline_bytes(self) -> bool:
        """True when at least one locked ref carries pixel bytes (inline-capable)."""
        return any(r.has_bytes for r in self.locked_references)

    def reference_set_hash(self) -> str:
        """A stable hash of the locked ref ids + version (episodic dedup, §8.2).

        Deterministic across runs (sorted ids), so the same locked set always maps
        to the same ``reference_set_hash`` the episodic store keys "what worked
        before" on.
        """
        ids = sorted(r.ref_id for r in self.locked_references)
        payload = f"{self.entity_key}@v{self.version}|" + "|".join(ids)
        return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324


__all__ = [
    "IdentityBundle",
    "LockedReference",
    "Pose",
    "centroid",
    "cosine",
]
