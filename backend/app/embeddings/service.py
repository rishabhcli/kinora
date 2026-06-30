"""The :class:`EmbeddingStore` facade — one object that wires the subsystem.

Composes an :class:`~app.embeddings.embedder.Embedder`, an
:class:`~app.embeddings.cache.InMemoryEmbeddingCache`, a
:class:`~app.embeddings.index.VectorIndex`, and an
:class:`~app.embeddings.identity.IdentityStore` into a single entry point. Most
callers only need this:

* :meth:`embed_image` / :meth:`embed_text` — cached, space-stamped embedding;
* :attr:`identity` — the identity store (verify / best_reference / references);
* :meth:`migrate_to` — swap the embedder to a new model and re-embed the cache;
* :meth:`compact` — run a maintenance pass over the index.

The composition root can build one of these lazily (the DashScope embedder is
only constructed when a provider is supplied), so importing this package never
touches the network — matching the rest of the backend's lazy-DI convention.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.embeddings.cache import (
    InMemoryEmbeddingCache,
    content_key_for_image,
    content_key_for_text,
    reembed_stale,
)
from app.embeddings.config import EmbeddingStoreSettings
from app.embeddings.embedder import Embedder, FakeEmbedder
from app.embeddings.identity import IdentityStore
from app.embeddings.index import InMemoryVectorIndex, VectorIndex
from app.embeddings.maintenance import CompactionReport, compact_index
from app.embeddings.models import ReembedReport
from app.embeddings.vectors import EmbeddingVector, VectorSpace

logger = get_logger("app.embeddings.service")


class EmbeddingStore:
    """A wired embeddings + identity vector store."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        settings: EmbeddingStoreSettings,
        index: VectorIndex | None = None,
        cache: InMemoryEmbeddingCache | None = None,
    ) -> None:
        self._embedder = embedder
        self._settings = settings
        self._index = index or InMemoryVectorIndex()
        self._cache = (
            cache
            if cache is not None
            else InMemoryEmbeddingCache(
                max_entries=settings.cache_max_entries if settings.cache_enabled else 0
            )
        )
        self._identity = IdentityStore(self._index, embedder, settings)

    # -- accessors ---------------------------------------------------------- #
    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def space(self) -> VectorSpace:
        return self._embedder.space

    @property
    def index(self) -> VectorIndex:
        return self._index

    @property
    def cache(self) -> InMemoryEmbeddingCache:
        return self._cache

    @property
    def identity(self) -> IdentityStore:
        return self._identity

    @property
    def settings(self) -> EmbeddingStoreSettings:
        return self._settings

    # -- cached embedding --------------------------------------------------- #
    async def embed_image(self, image_bytes: bytes, *, use_cache: bool = True) -> EmbeddingVector:
        key = content_key_for_image(image_bytes)
        if use_cache and self._settings.cache_enabled:
            hit = await self._cache.get(key, self._embedder.space)
            if hit is not None:
                return hit
        vec = (await self._embedder.embed_images([image_bytes]))[0]
        if use_cache and self._settings.cache_enabled:
            await self._cache.put(key, vec)
        return vec

    async def embed_text(self, text: str, *, use_cache: bool = True) -> EmbeddingVector:
        key = content_key_for_text(text)
        if use_cache and self._settings.cache_enabled:
            hit = await self._cache.get(key, self._embedder.space)
            if hit is not None:
                return hit
        vec = (await self._embedder.embed_texts([text]))[0]
        if use_cache and self._settings.cache_enabled:
            await self._cache.put(key, vec)
        return vec

    # -- migration + maintenance ------------------------------------------- #
    async def migrate_to(
        self,
        new_embedder: Embedder,
        *,
        resolve_source: Any,
        swap_active: bool = True,
    ) -> ReembedReport:
        """Re-embed the cache into ``new_embedder``'s space (model change).

        ``resolve_source(content_key) -> bytes|str|None`` recovers the original
        payload for each stale cached entry. When ``swap_active`` (default) the
        store's *active* embedder and identity store are repointed at the new
        model afterward, so subsequent embeds/verdicts use it. The vector index
        itself is left to a separate re-index (its records carry their own space
        and won't match the new query space until re-embedded), which a caller
        can drive via :meth:`reindex_namespace`.
        """
        report = await reembed_stale(
            self._cache, target=new_embedder, resolve_source=resolve_source
        )
        if swap_active:
            self._embedder = new_embedder
            self._identity = IdentityStore(self._index, new_embedder, self._settings)
            logger.info(
                "embedding_store_migrated",
                target_space=new_embedder.space.key,
                reembedded=report.reembedded,
                failed=report.failed,
            )
        return report

    async def compact(
        self,
        *,
        namespaces: Any = None,
        dedup_threshold: float = 0.985,
        keep_versions: int | None = None,
    ) -> CompactionReport:
        """Run a maintenance/compaction pass over the index."""
        report = await compact_index(
            self._index,
            namespaces=namespaces,
            dedup_threshold=dedup_threshold,
            keep_versions=keep_versions,
        )
        logger.info(
            "embedding_store_compacted",
            deduped=report.deduped,
            version_pruned=report.version_pruned,
            orphans=report.orphan_namespaces_dropped,
        )
        return report

    # -- factories ---------------------------------------------------------- #
    @classmethod
    def in_memory_fake(
        cls,
        *,
        settings: EmbeddingStoreSettings | None = None,
        seed: int = 0,
    ) -> EmbeddingStore:
        """Build a fully in-memory store with a deterministic fake embedder.

        The single entry point tests use: no network, no infra, reproducible.
        """
        cfg = settings or EmbeddingStoreSettings()
        space = VectorSpace(
            provider=cfg.provider,
            model=cfg.model,
            dimension=cfg.dimension,
            version=cfg.space_version,
        )
        return cls(embedder=FakeEmbedder(space, seed=seed), settings=cfg)

    @classmethod
    def from_dashscope(
        cls,
        *,
        provider: Any,
        app_settings: Any,
        index: VectorIndex | None = None,
    ) -> EmbeddingStore:
        """Build the production store from the round-1 EmbeddingProvider + Settings."""
        from app.embeddings.embedder import DashScopeEmbedder

        cfg = EmbeddingStoreSettings.from_settings(app_settings)
        embedder = DashScopeEmbedder.from_provider(provider, app_settings)
        return cls(embedder=embedder, settings=cfg, index=index)


__all__ = ["EmbeddingStore"]
