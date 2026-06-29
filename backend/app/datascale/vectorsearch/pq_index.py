"""A PQ/SQ-compressed flat index — the memory-frugal ANN tier.

Where :class:`~app.datascale.vectorsearch.hnsw.HnswIndex` keeps full ``float32``
vectors and a graph, this index keeps only **compressed codes** plus the trained
codebooks, so memory is ``m`` bytes/vector (PQ) or ``d`` bytes/vector (SQ)
instead of ``4d``. It answers a query in two stages — the textbook PQ recipe:

1. **Coarse, compressed scan.** Score every code with the PQ asymmetric distance
   table (one table build, then a gather-and-sum) or by decoding SQ codes. This
   is approximate but cache-friendly and needs no float vectors in RAM.
2. **Optional exact re-rank.** Take the top ``rerank`` candidates and, if the
   caller kept the originals (or a higher-precision store), re-score them
   exactly. With re-rank the recall climbs to near-exact while still touching
   only a small candidate set per query.

It must be **trained** on a representative sample before adding vectors (the
codebooks are data-dependent). :meth:`train` accepts that sample; :meth:`add`
then only stores codes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import distance as dist
from .filtering import Predicate
from .quantization import ProductQuantizer, ScalarQuantizer
from .types import FLOAT, Metadata, Metric, SearchResult, VectorId, as_vector


class QuantizedFlatIndex:
    """A compressed flat ANN index (PQ or SQ) with optional exact re-rank."""

    def __init__(
        self,
        dim: int,
        *,
        metric: Metric = Metric.COSINE,
        kind: str = "pq",
        m: int = 8,
        nbits: int = 8,
        sq_bits: int = 8,
        keep_originals: bool = True,
        seed: int = 0,
    ) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        if kind not in ("pq", "sq"):
            raise ValueError("kind must be 'pq' or 'sq'")
        self.dim = dim
        self.metric = metric
        self.kind = kind
        self.keep_originals = keep_originals
        self._pq = ProductQuantizer(m=m, nbits=nbits, seed=seed) if kind == "pq" else None
        self._sq = ScalarQuantizer(bits=sq_bits) if kind == "sq" else None
        self._codes: NDArray[np.integer[Any]] | None = None
        self._ids: list[VectorId] = []
        self._pos: dict[VectorId, int] = {}
        self._meta: dict[VectorId, Metadata] = {}
        # Optional full-precision store for exact re-rank (small float matrix).
        self._originals: NDArray[np.float32] | None = (
            np.empty((0, dim), dtype=FLOAT) if keep_originals else None
        )

    @property
    def is_trained(self) -> bool:
        if self.kind == "pq":
            return self._pq is not None and self._pq.is_fitted
        return self._sq is not None and self._sq.is_fitted

    def train(self, sample: Any) -> QuantizedFlatIndex:
        """Fit the codebooks on a representative ``(n, d)`` sample (normalised)."""
        raw = np.atleast_2d(np.asarray(sample, dtype=FLOAT))
        mat = dist.maybe_normalize_matrix(raw, self.metric)
        if self.kind == "pq":
            assert self._pq is not None
            self._pq.fit(mat)
        else:
            assert self._sq is not None
            self._sq.fit(mat)
        return self

    # -- mutation ----------------------------------------------------------- #
    def add(self, vid: VectorId, vector: Any, *, metadata: Metadata | None = None) -> None:
        if not self.is_trained:
            raise RuntimeError("index must be train()ed before add()")
        vec = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        code = self._encode_one(vec)
        if vid in self._pos:
            self._codes[self._pos[vid]] = code  # type: ignore[index]
            if self._originals is not None:
                self._originals[self._pos[vid]] = vec
        else:
            self._pos[vid] = len(self._ids)
            self._ids.append(vid)
            self._codes = (
                code[None, :]
                if self._codes is None or self._codes.size == 0
                else np.vstack([self._codes, code[None, :]])
            )
            if self._originals is not None:
                self._originals = (
                    vec[None, :]
                    if self._originals.size == 0
                    else np.vstack([self._originals, vec[None, :]])
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
        metas = metadatas or [None] * len(ids)
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.add(vid, vec, metadata=meta)

    def _encode_one(self, vec: NDArray[np.float32]) -> NDArray[np.integer[Any]]:
        if self.kind == "pq":
            assert self._pq is not None
            return self._pq.encode(vec[None, :])[0]
        assert self._sq is not None
        return self._sq.encode(vec[None, :])[0]

    def remove(self, vid: VectorId) -> bool:
        idx = self._pos.pop(vid, None)
        if idx is None or self._codes is None:
            return False
        last = len(self._ids) - 1
        if idx != last:
            moved = self._ids[last]
            self._ids[idx] = moved
            self._codes[idx] = self._codes[last]
            self._pos[moved] = idx
            if self._originals is not None:
                self._originals[idx] = self._originals[last]
        self._ids.pop()
        self._codes = self._codes[:last]
        if self._originals is not None:
            self._originals = self._originals[:last]
        self._meta.pop(vid, None)
        return True

    # -- query -------------------------------------------------------------- #
    def search(
        self,
        vector: Any,
        k: int = 10,
        *,
        rerank: int | None = None,
        where: Predicate | Mapping[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Compressed top-``k`` with optional exact re-rank of the top candidates.

        ``rerank`` (default ``max(k, 4k)`` when originals are kept) sets how many
        coarse candidates are exactly re-scored. ``rerank=0`` disables re-rank
        (pure compressed search).
        """
        if k <= 0 or not self._ids or self._codes is None:
            return []
        q = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        pred = Predicate.coerce(where)
        coarse = self._coarse_distances(q)  # smaller = closer (approx sq-L2)

        if pred is not None:
            mask = np.array([pred.matches(self._meta.get(vid)) for vid in self._ids], dtype=bool)
            idxs = np.flatnonzero(mask)
            if idxs.size == 0:
                return []
            coarse_sub = coarse[idxs]
        else:
            idxs = np.arange(len(self._ids))
            coarse_sub = coarse

        do_rerank = self._originals is not None and rerank != 0 and self.keep_originals
        if do_rerank:
            depth = rerank if rerank is not None else max(k * 4, k)
            depth = min(depth, idxs.size)
            top = idxs[np.argsort(coarse_sub, kind="stable")[:depth]]
            assert self._originals is not None
            exact = dist.order_value_batch(q, self._originals[top], self.metric)
            order = np.argsort(exact, kind="stable")[:k]
            chosen = top[order]
            return self._build(q, chosen, exact_for=top, exact_vals=exact)
        # No re-rank: rank directly by the coarse key.
        sel = np.argsort(coarse_sub, kind="stable")[:k]
        chosen = idxs[sel]
        return self._build_coarse(chosen, coarse[chosen])

    def _coarse_distances(self, q: NDArray[np.float32]) -> NDArray[np.float32]:
        assert self._codes is not None
        if self.kind == "pq":
            assert self._pq is not None
            return self._pq.adc_distances(q, self._codes)
        # SQ: decode and score exactly in the metric's ordering.
        assert self._sq is not None
        decoded = self._sq.decode(self._codes)
        return dist.order_value_batch(q, decoded, self.metric)

    def _build(
        self,
        q: NDArray[np.float32],
        chosen: NDArray[np.integer[Any]],
        *,
        exact_for: NDArray[np.integer[Any]],
        exact_vals: NDArray[np.float32],
    ) -> list[SearchResult]:
        lut = {int(i): float(v) for i, v in zip(exact_for, exact_vals, strict=True)}
        out: list[SearchResult] = []
        for i in chosen:
            ii = int(i)
            ov = lut[ii]
            vid = self._ids[ii]
            out.append(
                SearchResult(
                    id=vid,
                    distance=ov,
                    score=dist.order_to_score(ov, self.metric),
                    metadata=self._meta.get(vid),
                )
            )
        return out

    def _build_coarse(
        self, chosen: NDArray[np.integer[Any]], vals: NDArray[np.float32]
    ) -> list[SearchResult]:
        out: list[SearchResult] = []
        for i, v in zip(chosen, vals, strict=True):
            vid = self._ids[int(i)]
            # Coarse key is approximate squared-L2; expose it as the distance and
            # leave the native score derived from it (best effort under PQ).
            out.append(
                SearchResult(
                    id=vid,
                    distance=float(v),
                    score=float(-v) if self.metric.is_similarity else float(v),
                    metadata=self._meta.get(vid),
                )
            )
        return out

    # -- introspection ------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, vid: object) -> bool:
        return vid in self._pos

    def memory_bytes(self) -> int:
        """Approximate bytes held by the compressed codes (excludes originals)."""
        return 0 if self._codes is None else int(self._codes.nbytes)

    def compression_ratio(self) -> float:
        """float32 size ÷ compressed-code size (per vector)."""
        full = 4 * self.dim
        if self.kind == "pq":
            assert self._pq is not None
            comp = self._pq.m * (1 if self._pq.nbits <= 8 else 2)
        else:
            assert self._sq is not None
            comp = self.dim * (1 if self._sq.bits <= 8 else 2)
        return full / comp


__all__ = ["QuantizedFlatIndex"]
