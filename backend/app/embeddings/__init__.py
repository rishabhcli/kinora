"""Provider-agnostic embeddings / identity vector store (v2).

This package is the durable, queryable store *beneath* Round-1's cross-provider
identity conditioning (kinora.md §8 canon / identity-lock). Where the round-1
:mod:`app.providers.embeddings` client only *produces* vectors from one hosted
DashScope model, this subsystem makes vectors a first-class, versioned,
queryable asset:

* :class:`~app.embeddings.vectors.EmbeddingVector` — a canonical vector value
  type stamped with its ``space`` (model + dimension + version). Two vectors are
  only ever compared if their spaces match, so vectors from different embedders
  (DashScope ``tongyi-vision``, OpenAI, a local CLIP) can never be silently
  mixed.
* :class:`~app.embeddings.embedder.Embedder` — a multi-backend embedder
  abstraction. Real backends (DashScope, OpenAI, CLIP/local) embed image+text;
  :class:`~app.embeddings.embedder.FakeEmbedder` is a deterministic, seeded
  embedder for tests (no network).
* :class:`~app.embeddings.index.VectorIndex` — a pluggable index protocol with an
  exact in-memory implementation (:class:`~app.embeddings.index.InMemoryVectorIndex`)
  supporting upsert, k-NN, metadata filters, and per-book/entity namespaces. The
  interface is shaped so a pgvector/HNSW backend drops in unchanged.
* :class:`~app.embeddings.identity.IdentityStore` — holds per-character/setting
  reference images, their embeddings, and appearance descriptors with versioning;
  answers "is this new frame the same character?" (a similarity *verdict*) and
  "fetch the best reference for this pose/shot".
* :class:`~app.embeddings.cache.EmbeddingCache` — content-addressed cache with a
  re-embed-on-model-change migration path.
* :mod:`app.embeddings.maintenance` — compaction / garbage-collection ops.

Everything is additive and lives under this namespace. Nothing here flips the
``KINORA_LIVE_VIDEO`` spend gate or calls a network in tests.
"""

from __future__ import annotations

from app.embeddings.cache import CacheStats, EmbeddingCache, InMemoryEmbeddingCache
from app.embeddings.config import EmbeddingStoreSettings
from app.embeddings.embedder import (
    Embedder,
    EmbedRequest,
    FakeEmbedder,
    Modality,
)
from app.embeddings.identity import (
    IdentityStore,
    MatchVerdict,
    ReferenceImage,
    Verdict,
)
from app.embeddings.index import (
    InMemoryVectorIndex,
    MetadataFilter,
    SearchResult,
    VectorIndex,
    VectorRecord,
)
from app.embeddings.maintenance import CompactionReport, compact_index
from app.embeddings.models import EntityKind, ReembedReport
from app.embeddings.service import EmbeddingStore
from app.embeddings.vectors import (
    DimensionMismatch,
    EmbeddingVector,
    SpaceMismatch,
    VectorSpace,
    cosine,
)

__all__ = [
    "CacheStats",
    "CompactionReport",
    "DimensionMismatch",
    "Embedder",
    "EmbedRequest",
    "EmbeddingCache",
    "EmbeddingStore",
    "EmbeddingStoreSettings",
    "EmbeddingVector",
    "EntityKind",
    "FakeEmbedder",
    "IdentityStore",
    "InMemoryEmbeddingCache",
    "InMemoryVectorIndex",
    "MatchVerdict",
    "MetadataFilter",
    "Modality",
    "ReembedReport",
    "ReferenceImage",
    "SearchResult",
    "SpaceMismatch",
    "Verdict",
    "VectorIndex",
    "VectorRecord",
    "VectorSpace",
    "compact_index",
    "cosine",
]
