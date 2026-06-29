"""Distance / similarity kernels, NumPy-vectorised.

The whole index orders neighbours by a single "smaller is closer" key. The two
similarity metrics (cosine, dot) are therefore *negated* into that ordering by
:func:`order_value` / :func:`order_value_batch`, while the human-facing native
score is returned separately by the index. Centralising the sign convention here
keeps the HNSW graph, the brute-force baseline and the quantizers consistent.

``cosine`` is computed as a dot product on L2-normalised vectors; the index
normalises on insert when the metric is cosine so query time is a single dot.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from .types import FLOAT, Metric

_EPS = 1e-12


def normalize(vec: NDArray[np.float32]) -> NDArray[np.float32]:
    """L2-normalise a single vector (zero vector is returned unchanged)."""
    norm = float(np.linalg.norm(vec))
    if norm <= _EPS:
        return vec.astype(FLOAT, copy=True)
    return (vec / norm).astype(FLOAT, copy=False)


def normalize_matrix(mat: NDArray[np.float32]) -> NDArray[np.float32]:
    """Row-wise L2-normalise an ``(n, d)`` matrix (zero rows left unchanged)."""
    if mat.size == 0:
        return mat.astype(FLOAT, copy=False)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms <= _EPS, 1.0, norms)
    return (mat / norms).astype(FLOAT, copy=False)


def maybe_normalize(vec: NDArray[np.float32], metric: Metric) -> NDArray[np.float32]:
    """Normalise only when the metric is cosine (so cosine reduces to a dot)."""
    return normalize(vec) if metric is Metric.COSINE else vec.astype(FLOAT, copy=True)


def maybe_normalize_matrix(mat: NDArray[np.float32], metric: Metric) -> NDArray[np.float32]:
    """Row-wise normalise an ``(n, d)`` matrix only for the cosine metric."""
    return normalize_matrix(mat) if metric is Metric.COSINE else mat.astype(FLOAT, copy=False)


def native_score(a: NDArray[np.float32], b: NDArray[np.float32], metric: Metric) -> float:
    """The metric-native, human-facing score between two vectors.

    For cosine this assumes both vectors are already normalised (the index keeps
    them so) and returns their dot — i.e. the cosine in ``[-1, 1]``.
    """
    if metric is Metric.COSINE or metric is Metric.DOT:
        return float(np.dot(a, b))
    diff = a - b
    sq = float(np.dot(diff, diff))
    return sq if metric is Metric.L2SQ else float(np.sqrt(sq))


def native_score_batch(
    q: NDArray[np.float32], mat: NDArray[np.float32], metric: Metric
) -> NDArray[np.float32]:
    """Native score of ``q`` against every row of ``mat`` → ``(n,)`` vector."""
    if mat.size == 0:
        return np.empty((0,), dtype=FLOAT)
    if metric is Metric.COSINE or metric is Metric.DOT:
        return (mat @ q).astype(FLOAT, copy=False)
    diff = mat - q
    sq = np.einsum("ij,ij->i", diff, diff)
    return (sq if metric is Metric.L2SQ else np.sqrt(sq)).astype(FLOAT, copy=False)


def order_value(a: NDArray[np.float32], b: NDArray[np.float32], metric: Metric) -> float:
    """The "smaller is closer" ordering key (negated similarity for cos/dot)."""
    s = native_score(a, b, metric)
    return -s if metric.is_similarity else s


def order_value_batch(
    q: NDArray[np.float32], mat: NDArray[np.float32], metric: Metric
) -> NDArray[np.float32]:
    """Vectorised ordering key for ``q`` vs every row of ``mat``."""
    s = native_score_batch(q, mat, metric)
    return (-s if metric.is_similarity else s).astype(FLOAT, copy=False)


def score_to_order(score: float, metric: Metric) -> float:
    """Convert a native score to the ordering key (smaller is closer)."""
    return -score if metric.is_similarity else score


def order_to_score(order: float, metric: Metric) -> float:
    """Invert :func:`score_to_order` — recover the native score from the key."""
    return -order if metric.is_similarity else order


def pairwise_order(
    queries: NDArray[np.float32], mat: NDArray[np.float32], metric: Metric
) -> NDArray[np.float32]:
    """Ordering keys for an ``(m, d)`` query block vs ``(n, d)`` data → ``(m, n)``.

    Used by the batch / benchmark paths; computed with BLAS where possible.
    """
    if queries.size == 0 or mat.size == 0:
        return np.empty((queries.shape[0], mat.shape[0]), dtype=FLOAT)
    if metric is Metric.COSINE or metric is Metric.DOT:
        sim = queries @ mat.T
        return (-sim).astype(FLOAT, copy=False)
    # ||q - x||^2 = ||q||^2 + ||x||^2 - 2 q·x  (broadcast).
    qq = np.einsum("ij,ij->i", queries, queries)[:, None]
    xx = np.einsum("ij,ij->i", mat, mat)[None, :]
    sq = qq + xx - 2.0 * (queries @ mat.T)
    np.maximum(sq, 0.0, out=sq)  # guard tiny negatives from fp error
    return (sq if metric is Metric.L2SQ else np.sqrt(sq)).astype(FLOAT, copy=False)


def metric_of(name: str | Metric) -> Metric:
    """Coerce a string or :class:`Metric` to a :class:`Metric` (case-insensitive)."""
    if isinstance(name, Metric):
        return name
    try:
        return Metric(str(name).lower())
    except ValueError as exc:  # pragma: no cover - defensive
        valid = ", ".join(m.value for m in Metric)
        raise ValueError(f"unknown metric {name!r}; expected one of: {valid}") from exc


def random_unit_vectors(n: int, dim: int, *, seed: int | None = None) -> NDArray[np.float32]:
    """Deterministic random unit vectors — a test/benchmark data helper."""
    rng = np.random.default_rng(seed)
    mat: NDArray[Any] = rng.standard_normal((n, dim))
    return normalize_matrix(mat.astype(FLOAT, copy=False))


__all__ = [
    "maybe_normalize",
    "maybe_normalize_matrix",
    "metric_of",
    "native_score",
    "native_score_batch",
    "normalize",
    "normalize_matrix",
    "order_to_score",
    "order_value",
    "order_value_batch",
    "pairwise_order",
    "random_unit_vectors",
    "score_to_order",
]
