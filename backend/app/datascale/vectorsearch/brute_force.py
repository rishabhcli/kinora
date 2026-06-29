"""Exact, brute-force nearest-neighbour index — the ground truth for benchmarks.

This is intentionally simple and correct: it stores every vector in one matrix
and answers a query by scoring the whole set with a single BLAS call. It is the
reference an approximate index's recall is measured *against*
(:mod:`app.datascale.vectorsearch.benchmark`), and a perfectly valid backend for
small shards where O(n) is cheap.

Deletes are immediate (the row is dropped) so the exact set is always tight; the
HNSW index, by contrast, uses tombstones (deletion is a graph problem there).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import distance as dist
from .filtering import Predicate
from .types import FLOAT, Metadata, Metric, SearchResult, VectorId, as_vector


class BruteForceIndex:
    """Exact kNN by full scan. Correct by construction; O(n·d) per query."""

    def __init__(self, dim: int, *, metric: Metric = Metric.COSINE) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self.metric = metric
        self._ids: list[VectorId] = []
        self._pos: dict[VectorId, int] = {}
        self._vecs: NDArray[np.float32] = np.empty((0, dim), dtype=FLOAT)
        self._meta: dict[VectorId, Metadata] = {}

    # -- mutation ----------------------------------------------------------- #
    def add(self, vid: VectorId, vector: Any, *, metadata: Metadata | None = None) -> None:
        """Insert or replace ``vid``'s vector (and optional metadata)."""
        vec = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        if vid in self._pos:
            self._vecs[self._pos[vid]] = vec
        else:
            self._pos[vid] = len(self._ids)
            self._ids.append(vid)
            self._vecs = (
                vec[None, :] if self._vecs.size == 0 else np.vstack([self._vecs, vec[None, :]])
            )
        if metadata is not None:
            self._meta[vid] = dict(metadata)

    def add_many(
        self,
        ids: Sequence[VectorId],
        vectors: Any,
        *,
        metadatas: Sequence[Metadata | None] | None = None,
    ) -> None:
        """Bulk insert; metadatas (if given) must align with ``ids``."""
        metas = metadatas or [None] * len(ids)
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.add(vid, vec, metadata=meta)

    def remove(self, vid: VectorId) -> bool:
        """Delete ``vid``; returns False if it was absent."""
        idx = self._pos.pop(vid, None)
        if idx is None:
            return False
        last = len(self._ids) - 1
        if idx != last:  # swap-remove to keep the matrix dense
            moved = self._ids[last]
            self._ids[idx] = moved
            self._vecs[idx] = self._vecs[last]
            self._pos[moved] = idx
        self._ids.pop()
        self._vecs = self._vecs[:last]
        self._meta.pop(vid, None)
        return True

    # -- query -------------------------------------------------------------- #
    def search(
        self,
        vector: Any,
        k: int = 10,
        *,
        where: Predicate | Mapping[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Exact top-``k`` neighbours, optionally pre-filtered by metadata."""
        if k <= 0 or not self._ids:
            return []
        q = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        pred = Predicate.coerce(where)
        if pred is not None:
            mask = np.array([pred.matches(self._meta.get(vid)) for vid in self._ids], dtype=bool)
            idxs = np.flatnonzero(mask)
            if idxs.size == 0:
                return []
            sub = self._vecs[idxs]
            order = dist.order_value_batch(q, sub, self.metric)
            sel = np.argsort(order, kind="stable")[: min(k, idxs.size)]
            chosen = idxs[sel]
        else:
            order = dist.order_value_batch(q, self._vecs, self.metric)
            chosen = np.argsort(order, kind="stable")[: min(k, len(self._ids))]
        out: list[SearchResult] = []
        for i in chosen:
            vid = self._ids[int(i)]
            ov = float(dist.order_value(q, self._vecs[int(i)], self.metric))
            out.append(
                SearchResult(
                    id=vid,
                    distance=ov,
                    score=dist.order_to_score(ov, self.metric),
                    metadata=self._meta.get(vid),
                )
            )
        return out

    def exact_neighbors(self, vector: Any, k: int) -> list[VectorId]:
        """Just the ids of the exact top-``k`` (the recall ground-truth helper)."""
        return [r.id for r in self.search(vector, k)]

    # -- introspection ------------------------------------------------------ #
    def get(self, vid: VectorId) -> NDArray[np.float32] | None:
        idx = self._pos.get(vid)
        return None if idx is None else self._vecs[idx].copy()

    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, vid: object) -> bool:
        return vid in self._pos

    def ids(self) -> Iterable[VectorId]:
        return tuple(self._ids)


__all__ = ["BruteForceIndex"]
