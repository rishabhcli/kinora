"""Identity-consistency self-check — score an output crop's drift vs locked truth.

This is the conditioning loop's *closing the loop* hook (kinora.md §9.5 CCS — the
Critic's character-consistency score): after a clip renders, embed a crop of the
generated subject and compare it to the bundle's locked identity. High similarity
to the locked anchor ⇒ identity held; low ⇒ drift, which the Critic routes to a
re-render (§9.5 routing, MEMORY "identity drift").

The check is provider-agnostic and lives here (not in a provider) so *any* render
path — direct r2v, inline-image, or the keyframe fallback — runs the same drift
measurement against the same locked descriptor. It depends only on an injectable
:class:`CropEmbedder` seam (a slice of the embeddings provider) so it is unit-
testable with deterministic vectors and never touches the network in tests.

Drift is defined as ``1 - max_similarity`` where similarity is the cosine of the
crop descriptor against the bundle's anchor (the canon appearance embedding, else
the locked-ref centroid) *and* each individual locked ref — taking the best match
so an off-angle crop that matches the profile ref but not the front ref still
scores well.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger

from .bundle import IdentityBundle, cosine

logger = get_logger("app.video.identity.selfcheck")


# --------------------------------------------------------------------------- #
# The embedding seam
# --------------------------------------------------------------------------- #


@runtime_checkable
class CropEmbedder(Protocol):
    """The narrow embedding capability the self-check needs (injectable seam).

    Satisfied by ``app.providers.embeddings.EmbeddingProvider`` (its
    ``embed_images`` matches) and by a deterministic test double. Returns one
    unit-normalized vector per input image.
    """

    async def embed_images(self, images: list[bytes]) -> list[list[float]]: ...


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #


class DriftVerdict(StrEnum):
    """Coarse outcome of the self-check (maps to the Critic's routing, §9.5)."""

    #: Identity held — within tolerance. Accept.
    OK = "ok"
    #: Borderline — above ``warn`` but below ``fail``. Accept with a flag.
    WARN = "warn"
    #: Identity drifted past tolerance — route to re-render.
    FAIL = "fail"
    #: Could not measure (no anchor descriptor, or embedding failed). Caller
    #: decides; the self-check never *blocks* a render on its own inability.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DriftThresholds:
    """Similarity tolerances (cosine in ``[0,1]``; deterministic, no env reads).

    Attributes:
        fail_below: Similarity below this ⇒ :data:`DriftVerdict.FAIL`.
        warn_below: Similarity below this (but ≥ ``fail_below``) ⇒ ``WARN``.
    """

    fail_below: float = 0.75
    warn_below: float = 0.85

    def __post_init__(self) -> None:
        if not 0.0 <= self.fail_below <= self.warn_below <= 1.0:
            raise ValueError("require 0 <= fail_below <= warn_below <= 1")


@dataclass(frozen=True, slots=True)
class DriftReport:
    """The result of one consistency self-check.

    Attributes:
        entity_key: The entity checked.
        similarity: Best cosine of the crop vs {anchor, each locked ref} in [0,1].
        drift: ``1 - similarity`` — higher = more drift (the score the rubric/QA
            cares about; mirrors the §9.5 ``style_drift`` shape).
        verdict: Coarse routing outcome.
        anchor_similarity: Cosine against the bundle anchor specifically.
        best_ref_id: The locked ref id that matched best (None if anchor won/empty).
        measured: Whether a real measurement happened (False ⇒ UNKNOWN).
    """

    entity_key: str
    similarity: float
    drift: float
    verdict: DriftVerdict
    anchor_similarity: float
    best_ref_id: str | None
    measured: bool

    @property
    def passed(self) -> bool:
        """True when the verdict is acceptable (OK or WARN)."""
        return self.verdict in (DriftVerdict.OK, DriftVerdict.WARN)


def score_descriptor(
    bundle: IdentityBundle,
    crop_descriptor: tuple[float, ...] | list[float],
    *,
    thresholds: DriftThresholds | None = None,
) -> DriftReport:
    """Score a crop descriptor against a bundle's locked identity (pure, no I/O).

    The best similarity over {anchor, every locked ref descriptor} is taken so an
    off-angle crop is judged against its closest locked pose. Returns
    :data:`DriftVerdict.UNKNOWN` when there is nothing to compare against (no
    anchor and no ref descriptors, or an empty crop vector) rather than failing.
    """
    th = thresholds or DriftThresholds()
    crop = tuple(crop_descriptor)
    anchor = bundle.anchor_descriptor

    candidates: list[tuple[float, str | None]] = []
    if anchor:
        candidates.append((cosine(crop, anchor), None))
    for ref in bundle.locked_references:
        if ref.has_descriptor:
            candidates.append((cosine(crop, ref.descriptor), ref.ref_id))

    if not crop or not candidates:
        return DriftReport(
            entity_key=bundle.entity_key,
            similarity=0.0,
            drift=1.0,
            verdict=DriftVerdict.UNKNOWN,
            anchor_similarity=0.0,
            best_ref_id=None,
            measured=False,
        )

    best_sim, best_ref = max(candidates, key=lambda c: c[0])
    anchor_sim = cosine(crop, anchor) if anchor else 0.0
    # Clamp negatives (anti-correlated) to 0 — drift caps at 1.0.
    best_sim = max(best_sim, 0.0)
    if best_sim < th.fail_below:
        verdict = DriftVerdict.FAIL
    elif best_sim < th.warn_below:
        verdict = DriftVerdict.WARN
    else:
        verdict = DriftVerdict.OK
    return DriftReport(
        entity_key=bundle.entity_key,
        similarity=best_sim,
        drift=round(1.0 - best_sim, 6),
        verdict=verdict,
        anchor_similarity=max(anchor_sim, 0.0),
        best_ref_id=best_ref,
        measured=True,
    )


class IdentitySelfCheck:
    """Embed an output crop and score its drift vs a locked identity bundle.

    Wraps :func:`score_descriptor` with the embedding seam so callers hand over raw
    crop bytes. Embedding failure degrades to :data:`DriftVerdict.UNKNOWN` (the
    self-check never sinks a render by its own inability to measure).
    """

    def __init__(
        self,
        embedder: CropEmbedder,
        *,
        thresholds: DriftThresholds | None = None,
    ) -> None:
        self._embedder = embedder
        self._thresholds = thresholds or DriftThresholds()

    async def check(self, bundle: IdentityBundle, crop_bytes: bytes) -> DriftReport:
        """Embed ``crop_bytes`` and score it against ``bundle``'s locked identity."""
        try:
            vectors = await self._embedder.embed_images([crop_bytes])
        except Exception:  # noqa: BLE001 — embedding failure ⇒ UNKNOWN, not a crash
            logger.warning("identity.selfcheck.embed_failed", entity=bundle.entity_key)
            return _unknown(bundle.entity_key)
        if not vectors or not vectors[0]:
            return _unknown(bundle.entity_key)
        report = score_descriptor(
            bundle, tuple(vectors[0]), thresholds=self._thresholds
        )
        logger.info(
            "identity.selfcheck.scored",
            entity=bundle.entity_key,
            similarity=round(report.similarity, 4),
            drift=report.drift,
            verdict=report.verdict.value,
        )
        return report


def _unknown(entity_key: str) -> DriftReport:
    return DriftReport(
        entity_key=entity_key,
        similarity=0.0,
        drift=1.0,
        verdict=DriftVerdict.UNKNOWN,
        anchor_similarity=0.0,
        best_ref_id=None,
        measured=False,
    )


__all__ = [
    "CropEmbedder",
    "DriftReport",
    "DriftThresholds",
    "DriftVerdict",
    "IdentitySelfCheck",
    "score_descriptor",
]
