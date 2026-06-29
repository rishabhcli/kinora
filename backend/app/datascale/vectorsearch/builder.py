"""Index build orchestration — batch and incremental.

Two build modes the prompt calls for:

* **Batch build** (:func:`build_hnsw`, :func:`build_sharded`,
  :func:`build_quantized`) — construct an index from a full ``(ids, vectors,
  metadatas)`` dataset in one pass, with sensible auto-tuned defaults. For the
  quantized index this also trains the codebooks on a sample first.
* **Incremental build** (:class:`IncrementalBuilder`) — a long-lived builder
  that accepts micro-batches over time (the streaming-ingest shape: shots are
  logged as they render, §8.2), periodically compacting once tombstones from
  re-renders pile up. It tracks a dirty/since-compaction counter and exposes a
  ``maybe_compact`` policy so callers don't hand-roll it.

The builders return the concrete index objects; wiring them to the service is
:class:`~app.datascale.vectorsearch.service.VectorSearchService`'s job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .hnsw import HnswIndex, HnswParams
from .pq_index import QuantizedFlatIndex
from .shard import Router, ShardedIndex
from .types import FLOAT, Metadata, Metric, VectorId, as_matrix


def auto_params(n: int, *, base: HnswParams | None = None) -> HnswParams:
    """Heuristic HNSW params for a dataset of size ``n``.

    Larger collections want a wider construction beam and degree for recall;
    tiny ones can stay cheap. These are conservative, well-tested defaults — a
    caller can always pass explicit params.
    """
    if base is not None:
        return base
    if n < 1_000:
        return HnswParams(m=8, ef_construction=100, ef_search=32)
    if n < 100_000:
        return HnswParams(m=16, ef_construction=200, ef_search=64)
    return HnswParams(m=32, ef_construction=400, ef_search=128)


def build_hnsw(
    ids: Sequence[VectorId],
    vectors: Any,
    *,
    dim: int | None = None,
    metric: Metric = Metric.COSINE,
    params: HnswParams | None = None,
    metadatas: Sequence[Metadata | None] | None = None,
) -> HnswIndex:
    """Batch-build an :class:`HnswIndex` from a dataset."""
    mat = as_matrix(list(vectors), dim=dim)
    d = dim if dim is not None else (mat.shape[1] if mat.size else 0)
    index = HnswIndex(
        d, metric=metric, params=auto_params(len(ids), base=params), capacity=max(len(ids), 1)
    )
    metas: list[Metadata | None] = list(metadatas) if metadatas is not None else [None] * len(ids)
    index.add_many(list(ids), [mat[i] for i in range(mat.shape[0])], metadatas=metas)
    return index


def build_sharded(
    ids: Sequence[VectorId],
    vectors: Any,
    *,
    n_shards: int = 4,
    dim: int | None = None,
    metric: Metric = Metric.COSINE,
    params: HnswParams | None = None,
    router: Router | None = None,
    metadatas: Sequence[Metadata | None] | None = None,
) -> ShardedIndex:
    """Batch-build a :class:`ShardedIndex` (routes each vector then inserts)."""
    mat = as_matrix(list(vectors), dim=dim)
    d = dim if dim is not None else (mat.shape[1] if mat.size else 0)
    index = ShardedIndex(
        d,
        n_shards=n_shards,
        metric=metric,
        params=auto_params(len(ids) // max(n_shards, 1), base=params),
        router=router,
    )
    metas: list[Metadata | None] = list(metadatas) if metadatas is not None else [None] * len(ids)
    index.add_many(list(ids), [mat[i] for i in range(mat.shape[0])], metadatas=metas)
    return index


def build_quantized(
    ids: Sequence[VectorId],
    vectors: Any,
    *,
    dim: int | None = None,
    metric: Metric = Metric.COSINE,
    kind: str = "pq",
    m: int = 8,
    nbits: int = 8,
    sq_bits: int = 8,
    keep_originals: bool = True,
    sample_size: int = 4096,
    seed: int = 0,
    metadatas: Sequence[Metadata | None] | None = None,
) -> QuantizedFlatIndex:
    """Train codebooks on a sample, then batch-encode the dataset."""
    mat = as_matrix(list(vectors), dim=dim)
    d = dim if dim is not None else (mat.shape[1] if mat.size else 0)
    index = QuantizedFlatIndex(
        d,
        metric=metric,
        kind=kind,
        m=m,
        nbits=nbits,
        sq_bits=sq_bits,
        keep_originals=keep_originals,
        seed=seed,
    )
    if mat.shape[0]:
        sample = _sample_rows(mat, sample_size, seed)
        index.train(sample)
        metas: list[Metadata | None] = (
            list(metadatas) if metadatas is not None else [None] * len(ids)
        )
        index.add_many(list(ids), [mat[i] for i in range(mat.shape[0])], metadatas=metas)
    return index


def _sample_rows(mat: NDArray[np.float32], n: int, seed: int) -> NDArray[np.float32]:
    if mat.shape[0] <= n:
        return mat
    rng = np.random.default_rng(seed)
    idx = rng.choice(mat.shape[0], size=n, replace=False)
    return mat[idx].astype(FLOAT, copy=False)


@dataclass(slots=True)
class IncrementalBuilder:
    """A long-lived builder for streaming inserts with a compaction policy.

    Feed micro-batches via :meth:`add` / :meth:`add_batch`; call :meth:`remove`
    for retired vectors. After enough churn (``compact_after`` tombstones, or a
    tombstone fraction over ``compact_fraction``) :meth:`maybe_compact` rebuilds
    the underlying graph to reclaim slots. ``index`` is always a live, queryable
    :class:`HnswIndex`.
    """

    dim: int
    metric: Metric = Metric.COSINE
    params: HnswParams | None = None
    compact_after: int = 1000
    compact_fraction: float = 0.2
    index: HnswIndex = field(init=False)
    _added: int = field(default=0, init=False)
    _removed_since: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.index = HnswIndex(self.dim, metric=self.metric, params=self.params or HnswParams())

    def add(self, vid: VectorId, vector: Any, *, metadata: Metadata | None = None) -> None:
        self.index.add(vid, vector, metadata=metadata)
        self._added += 1

    def add_batch(
        self,
        ids: Sequence[VectorId],
        vectors: Any,
        *,
        metadatas: Sequence[Metadata | None] | None = None,
    ) -> None:
        metas: list[Metadata | None] = (
            list(metadatas) if metadatas is not None else [None] * len(ids)
        )
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.add(vid, vec, metadata=meta)

    def remove(self, vid: VectorId) -> bool:
        ok = self.index.remove(vid)
        if ok:
            self._removed_since += 1
        return ok

    def should_compact(self) -> bool:
        live = len(self.index)
        deleted = self.index.num_deleted
        if deleted == 0:
            return False
        if self._removed_since >= self.compact_after:
            return True
        total = live + deleted
        return total > 0 and (deleted / total) >= self.compact_fraction

    def maybe_compact(self) -> bool:
        """Compact if the policy says so. Returns whether a compaction ran."""
        if self.should_compact():
            self.index = self.index.compact()
            self._removed_since = 0
            return True
        return False

    def force_compact(self) -> None:
        self.index = self.index.compact()
        self._removed_since = 0


__all__ = [
    "IncrementalBuilder",
    "auto_params",
    "build_hnsw",
    "build_quantized",
    "build_sharded",
]
