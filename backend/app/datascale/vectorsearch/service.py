"""``VectorSearchService`` — the clean, backend-agnostic query API.

This is the single entry point a caller (or the MCP ``episodic.search`` tool,
§8.3) uses. It owns:

* **Backend selection** from :class:`~app.datascale.vectorsearch.config.VectorSearchConfig`
  — flat HNSW, sharded HNSW, a PQ/SQ compressed index, or brute force — behind
  one uniform ``upsert`` / ``search`` / ``delete`` surface.
* **Hybrid search** — a dense ANN run fused with an optional BM25 keyword run
  over a configured metadata text field, plus metadata pre/post-filtering. The
  vector path supplies recall; the keyword path rescues exact-term matches the
  embedding glossed over; metadata filters scope both.
* **A typed result** (:class:`SearchResult`) and a structured :class:`Query`.

It deliberately speaks plain ``list[float]`` / ``str`` ids at the boundary so it
composes with the existing ``app.lib`` / MCP layers without dragging NumPy into
their type signatures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .brute_force import BruteForceIndex
from .builder import auto_params
from .config import VectorSearchConfig
from .filtering import (
    Bm25KeywordIndex,
    Predicate,
    cosine_to_unit,
    fuse_scores,
    tokenize,
)
from .hnsw import HnswIndex, HnswParams
from .merge import merge_dedup_keep_closest
from .pq_index import QuantizedFlatIndex
from .shard import Router, ShardedIndex
from .types import Metadata, Metric, Query, SearchResult, VectorId


class VectorSearchService:
    """A high-level vector-search facade over a pluggable ANN backend."""

    def __init__(
        self,
        config: VectorSearchConfig | None = None,
        *,
        router: Router | None = None,
    ) -> None:
        self.config = config or VectorSearchConfig()
        self._keyword = Bm25KeywordIndex()
        self._has_keywords = False
        self._index = self._make_backend(router)

    # -- backend construction ---------------------------------------------- #
    def _hnsw_params(self) -> HnswParams:
        return HnswParams(
            m=self.config.m,
            ef_construction=self.config.ef_construction,
            ef_search=self.config.ef_search,
            seed=self.config.seed,
        )

    def _make_backend(self, router: Router | None) -> Any:
        c = self.config
        if c.backend == "hnsw":
            return HnswIndex(c.dim, metric=c.metric, params=self._hnsw_params())
        if c.backend == "sharded":
            return ShardedIndex(
                c.dim,
                n_shards=c.n_shards,
                metric=c.metric,
                params=self._hnsw_params(),
                router=router,
            )
        if c.backend in ("pq", "sq"):
            return QuantizedFlatIndex(
                c.dim,
                metric=c.metric,
                kind=c.backend,
                m=c.pq_m,
                nbits=c.pq_nbits,
                sq_bits=c.sq_bits,
                keep_originals=c.keep_originals,
                seed=c.seed,
            )
        return BruteForceIndex(c.dim, metric=c.metric)

    @property
    def backend_kind(self) -> str:
        return self.config.backend

    @property
    def index(self) -> Any:
        """The underlying index object (for advanced / persistence use)."""
        return self._index

    # -- training (quantized backends) ------------------------------------- #
    def train(self, sample: Sequence[Sequence[float]]) -> None:
        """Train a quantized backend's codebooks. No-op for non-quantized backends."""
        if isinstance(self._index, QuantizedFlatIndex):
            self._index.train(list(sample))

    @property
    def needs_training(self) -> bool:
        return isinstance(self._index, QuantizedFlatIndex) and not self._index.is_trained

    # -- mutation ----------------------------------------------------------- #
    def upsert(
        self,
        vid: VectorId,
        vector: Sequence[float],
        *,
        metadata: Metadata | None = None,
    ) -> None:
        """Insert or replace one vector + metadata, updating the keyword index."""
        self._index.add(vid, vector, metadata=metadata)
        self._index_keywords(vid, metadata)

    def upsert_many(
        self,
        ids: Sequence[VectorId],
        vectors: Sequence[Sequence[float]],
        *,
        metadatas: Sequence[Metadata | None] | None = None,
    ) -> None:
        metas = list(metadatas) if metadatas is not None else [None] * len(ids)
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.upsert(vid, vec, metadata=meta)

    def delete(self, vid: VectorId) -> bool:
        self._keyword.remove(vid)
        return self._index.remove(vid)

    def _index_keywords(self, vid: VectorId, metadata: Metadata | None) -> None:
        if not metadata:
            return
        text = metadata.get(self.config.keyword_field)
        if isinstance(text, str) and text:
            self._keyword.add_text(vid, text)
            self._has_keywords = True
        elif isinstance(text, (list, tuple)):
            self._keyword.add(vid, [str(t) for t in text])
            self._has_keywords = True

    # -- query -------------------------------------------------------------- #
    def search(
        self,
        vector: Sequence[float],
        k: int = 10,
        *,
        where: Mapping[str, Any] | None = None,
        ef: int | None = None,
    ) -> list[SearchResult]:
        """Pure ANN search (with optional metadata filter)."""
        return self._raw_search(vector, k, where=where, ef=ef)

    def _raw_search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        where: Mapping[str, Any] | None,
        ef: int | None,
    ) -> list[SearchResult]:
        idx = self._index
        if isinstance(idx, HnswIndex):
            return idx.search(vector, k, ef=ef, where=where)
        if isinstance(idx, ShardedIndex):
            return idx.search(vector, k, ef=ef, where=where)
        if isinstance(idx, QuantizedFlatIndex):
            return idx.search(vector, k, where=where)
        return idx.search(vector, k, where=where)  # BruteForceIndex

    def query(self, q: Query) -> list[SearchResult]:
        """Run a structured :class:`Query` — the full hybrid path.

        Fuses the dense ANN run with a BM25 keyword run when ``q.text`` /
        ``q.keywords`` is given and a keyword field has been indexed; otherwise
        it is a pure ANN search. ``q.alpha`` weights dense vs lexical.
        """
        use_keywords = (
            self._has_keywords and (q.text is not None or q.keywords is not None) and q.alpha < 1.0
        )
        if not use_keywords:
            return self._raw_search(list(q.vector), q.k, where=q.where, ef=q.ef)
        return self._hybrid(q)

    def _hybrid(self, q: Query) -> list[SearchResult]:
        # Oversample the dense run so fusion has a healthy candidate pool.
        pool = max(q.k * 4, q.k)
        dense_results = self._raw_search(list(q.vector), pool, where=q.where, ef=q.ef)
        dense_scores = {
            r.id: (
                cosine_to_unit(r.score)
                if self.config.metric is Metric.COSINE
                else _rank_to_unit(i, len(dense_results))
            )
            for i, r in enumerate(dense_results)
        }
        by_id = {r.id: r for r in dense_results}

        tokens: list[str] = list(q.keywords) if q.keywords else []
        if q.text:
            tokens += tokenize(q.text)
        lexical_all = self._keyword.score_normalised(tokens)
        # Respect the metadata filter on the lexical side too.
        pred = Predicate.coerce(q.where)
        lexical = {
            vid: s
            for vid, s in lexical_all.items()
            if pred is None or pred.matches(self._metadata_of(vid))
        }

        fused = fuse_scores(dense_scores, lexical, alpha=q.alpha)
        # Build results, pulling metadata where we have it; lexical-only hits get
        # a fresh lookup so an exact keyword match the ANN missed still returns.
        results: list[SearchResult] = []
        for vid, score in fused.items():
            existing = by_id.get(vid)
            meta = existing.metadata if existing else self._metadata_of(vid)
            results.append(SearchResult(id=vid, distance=-score, score=score, metadata=meta))
        # distance is the negated fused score → closest-first ordering.
        return merge_dedup_keep_closest([results], q.k)

    def _metadata_of(self, vid: VectorId) -> Metadata | None:
        idx = self._index
        getter = getattr(idx, "_meta", None)
        if isinstance(getter, dict):
            # HNSW keys _meta by internal node id; resolve via the id map.
            node_map = getattr(idx, "_ext_to_node", None)
            if node_map is not None:
                node = node_map.get(vid)
                return getter.get(node) if node is not None else None
            return getter.get(vid)
        return None

    # -- introspection ------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, vid: object) -> bool:
        return vid in self._index

    def compact(self) -> None:
        idx = self._index
        if isinstance(idx, HnswIndex):
            self._index = idx.compact()
        elif isinstance(idx, ShardedIndex):
            idx.compact()


def _rank_to_unit(rank: int, n: int) -> float:
    """Map a 0-based rank to ``[0, 1]`` (rank 0 → 1.0) for non-cosine fusion."""
    if n <= 1:
        return 1.0
    return 1.0 - rank / (n - 1)


__all__ = ["VectorSearchService", "auto_params"]
