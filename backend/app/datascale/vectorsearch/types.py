"""Core value types for the vector-search service.

Everything downstream speaks these types: an opaque string ``VectorId`` (so a
caller can key rows by ``shot_id`` / ``entity_id`` from §8), a ``Metadata`` dict
for filtering, and a ``SearchResult`` carrying the id, distance, score and
(optionally) the payload metadata.

Vectors are kept as ``numpy`` ``float32`` arrays internally for speed and a
compact, mmap-friendly on-disk layout; the public API also accepts plain
``Sequence[float]`` and converts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import NDArray

#: An opaque external identifier for a vector (e.g. a ``shot_id`` or ``char_id``).
VectorId = str

#: Arbitrary JSON-ish metadata attached to a vector, used for hybrid filtering.
Metadata = Mapping[str, Any]

#: A single dense vector in the public API (accepted as any float sequence).
VectorLike = Sequence[float] | NDArray[np.floating[Any]]

#: The internal storage dtype — float32 keeps memory and disk small and is the
#: pgvector/BLAS-friendly default.
FLOAT = np.float32


class Metric(StrEnum):
    """Supported distance / similarity metrics.

    ``COSINE`` and ``DOT`` are *similarity* (higher = closer); ``L2`` and
    ``L2SQ`` are *distance* (lower = closer). The index normalises everything to
    a single "smaller is closer" ordering internally via :func:`is_distance`.
    """

    COSINE = "cosine"
    DOT = "dot"
    L2 = "l2"
    L2SQ = "l2sq"

    @property
    def is_distance(self) -> bool:
        """True when smaller raw values mean *closer* (L2 family)."""
        return self in (Metric.L2, Metric.L2SQ)

    @property
    def is_similarity(self) -> bool:
        """True when larger raw values mean *closer* (cosine / dot)."""
        return not self.is_distance


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One neighbour returned from a query.

    ``distance`` is always the metric's *ordering* value where **smaller is
    closer** (cosine/dot similarities are negated so a single ``<`` orders every
    metric). ``score`` is the human-facing, metric-native number (cosine in
    ``[-1, 1]``, dot product, or L2 distance) so callers don't have to undo the
    sign flip. ``metadata`` is the stored payload when the index keeps it.
    """

    id: VectorId
    distance: float
    score: float
    metadata: Metadata | None = None

    def __lt__(self, other: SearchResult) -> bool:
        # Closer-first ordering for heaps / sorted().
        return self.distance < other.distance


@dataclass(frozen=True, slots=True)
class Query:
    """A query specification understood by the high-level service.

    Only ``vector`` is required for a pure-ANN search. The optional fields drive
    hybrid search: ``where`` is a metadata predicate (see
    :mod:`app.datascale.vectorsearch.filtering`), ``text`` / ``keywords`` add a
    lexical signal fused with the dense score, and ``ef`` overrides the index's
    search-time breadth for this query only.
    """

    vector: VectorLike
    k: int = 10
    where: Mapping[str, Any] | None = None
    text: str | None = None
    keywords: Sequence[str] | None = None
    ef: int | None = None
    #: Weight on the dense (vector) signal when fusing with the lexical signal.
    alpha: float = 1.0
    metadata_fields: Sequence[str] | None = field(default=None)


def as_vector(v: VectorLike, *, dim: int | None = None) -> NDArray[np.float32]:
    """Coerce any vector-like into a contiguous ``float32`` array.

    Raises :class:`ValueError` on a dimension mismatch (when ``dim`` is given) or
    on a non-1-D / non-finite input — failing loudly beats silently indexing a
    NaN that would poison every later distance.
    """
    arr = np.ascontiguousarray(v, dtype=FLOAT)
    if arr.ndim != 1:
        raise ValueError(f"vector must be 1-D, got shape {arr.shape}")
    if dim is not None and arr.shape[0] != dim:
        raise ValueError(f"vector dim mismatch: got {arr.shape[0]}, expected {dim}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("vector contains NaN or inf")
    return arr


def as_matrix(vectors: Sequence[VectorLike], *, dim: int | None = None) -> NDArray[np.float32]:
    """Stack vector-likes into an ``(n, d)`` ``float32`` matrix with validation."""
    if not vectors:
        d = dim if dim is not None else 0
        return np.empty((0, d), dtype=FLOAT)
    rows = [as_vector(v, dim=dim) for v in vectors]
    d0 = rows[0].shape[0]
    if any(r.shape[0] != d0 for r in rows):
        raise ValueError("all vectors must share one dimension")
    return np.vstack(rows).astype(FLOAT, copy=False)


__all__ = [
    "FLOAT",
    "Metadata",
    "Metric",
    "Query",
    "SearchResult",
    "VectorId",
    "VectorLike",
    "as_matrix",
    "as_vector",
]
