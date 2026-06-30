"""Reproducibility & seed-lineage for AI-video renders (provenance across providers).

Visual consistency across a long adaptation is Kinora's whole product, and that
consistency has a forensic side: **we must know exactly what produced any clip,
and re-rendering a shot must be reproducible where the model allows it.** This
package is that forensic layer, built additively beside the live render pipeline
(:mod:`app.render`) — it records and reasons, it never renders.

Four cooperating pieces:

* :class:`RenderFingerprint` (:mod:`.fingerprint`) — the full provenance manifest
  for one clip: the canonical request digest, resolved provider/model/version,
  seed, prompt-dialect + reference-identity digests, planner concessions, and
  post-ops, folded into a stable, collision-resistant ``fingerprint_id``. It can
  also emit the exact §8.7 ``shot_hash`` so a fingerprint and the cache key never
  disagree.
* :class:`SeedLineage` (:mod:`.seedtree`) — a deterministic seed tree derived from
  a book/scene root seed, so the *same* logical shot re-renders with the *same*
  seed (reproducible re-reads / surgical re-renders) while sibling shots stay
  coherently spread (collision-resistant, no clustering).
* :class:`RenderReplay` (:mod:`.replay`) — reconstruct the exact provider request
  from a fingerprint (re-resolving expired reference URLs through a seam), and
  state whether re-issuing yields the identical clip, the same plan, or a fresh
  roll. No I/O, no spend.
* :func:`diff_fingerprints` (:mod:`.diff`) — explain *why* two clips differ by
  attributing the difference to the fingerprint field that changed, ranked by how
  strongly it bears on the output.

Underpinning them: a per-provider :class:`DeterminismClassifier`
(:mod:`.classifier`) that labels each ``(provider, model)`` as **GUARANTEED** /
**BEST_EFFORT** / **NONE** reproducible, and canonical digest primitives
(:mod:`.hashing`) that are deterministic, order-insensitive where order is not
meaning, boundary-unambiguous, and type-faithful.

Nothing here imports the live providers' render path or touches
``KINORA_LIVE_VIDEO``; everything is pure and deterministically testable.
"""

from __future__ import annotations

from .classifier import (
    DEFAULT_CLASSIFIER,
    ByteStability,
    DeterminismClassification,
    DeterminismClassifier,
    DeterminismProfile,
    ReproLabel,
    SeedHonoring,
)
from .diff import (
    Causality,
    ChangeKind,
    FieldChange,
    FingerprintDiff,
    diff_fingerprints,
)
from .fingerprint import (
    FINGERPRINT_SCHEMA_VERSION,
    Concession,
    PostOp,
    RenderFingerprint,
    ResolvedProvider,
)
from .hashing import canonical_text, digest, digest_fields, join_digest
from .replay import ReferenceResolver, RenderReplay, ReplayPlan
from .seedtree import SEED_MASK, SeedLineage, SeedNode, root_seed

__all__ = [
    "DEFAULT_CLASSIFIER",
    "FINGERPRINT_SCHEMA_VERSION",
    "SEED_MASK",
    "ByteStability",
    "Causality",
    "ChangeKind",
    "Concession",
    "DeterminismClassification",
    "DeterminismClassifier",
    "DeterminismProfile",
    "FieldChange",
    "FingerprintDiff",
    "PostOp",
    "ReferenceResolver",
    "RenderFingerprint",
    "RenderReplay",
    "ReplayPlan",
    "ReproLabel",
    "ResolvedProvider",
    "SeedHonoring",
    "SeedLineage",
    "SeedNode",
    "diff_fingerprints",
    "digest",
    "canonical_text",
    "digest_fields",
    "join_digest",
    "root_seed",
]
