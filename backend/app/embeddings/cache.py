"""Content-addressed embedding cache + re-embed-on-model-change migration.

Embedding calls are the expensive part of the store (network + spend), so we
cache by **content** and **space**: the key is ``hash(modality, payload)`` and
the value is keyed within that by :attr:`VectorSpace.key`. This means:

* the same image/text re-embedded with the same model is a cache hit;
* when the model changes (a new :class:`~app.embeddings.vectors.VectorSpace`),
  the old entry is *not* a hit — it stays, tagged with its old space, so a
  migration can find and re-embed it, and the new vector is cached alongside.

:class:`EmbeddingCache` is a protocol; :class:`InMemoryEmbeddingCache` is the
deterministic, optionally-LRU-bounded implementation used by tests and offline
paths. A Redis-backed cache satisfies the same protocol.

:func:`reembed_stale` is the migration: given a *target* embedder (the new
model) and a source-resolver (content key -> original bytes/text), it finds
cached entries not in the target space, recomputes them, and caches the new
vectors — returning a :class:`~app.embeddings.models.ReembedReport`.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.embeddings.embedder import Embedder
from app.embeddings.models import Modality, ReembedReport
from app.embeddings.vectors import EmbeddingVector, VectorSpace


def content_key(modality: Modality, payload: bytes) -> str:
    """Stable content address for a (modality, payload) pair."""
    h = hashlib.sha256()
    h.update(modality.value.encode("ascii"))
    h.update(b"\x00")
    h.update(payload)
    return f"{modality.value}:{h.hexdigest()}"


def content_key_for_text(text: str) -> str:
    return content_key(Modality.TEXT, text.encode("utf-8"))


def content_key_for_image(image_bytes: bytes) -> str:
    return content_key(Modality.IMAGE, image_bytes)


@dataclass(slots=True)
class CacheStats:
    """Mutable hit/miss counters for observability + tests."""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


@runtime_checkable
class EmbeddingCache(Protocol):
    """A content+space addressed vector cache."""

    async def get(self, key: str, space: VectorSpace) -> EmbeddingVector | None:
        ...

    async def put(self, key: str, vector: EmbeddingVector) -> None:
        ...

    async def keys(self) -> list[str]:
        """All content keys with at least one cached space."""
        ...

    async def spaces_for(self, key: str) -> list[VectorSpace]:
        """Which spaces a content key has cached vectors for."""
        ...


class InMemoryEmbeddingCache:
    """In-memory cache, LRU-bounded per ``max_entries`` (0 = unbounded).

    Layout: ``{content_key: {space_key: EmbeddingVector}}``. The LRU bound counts
    *content keys* (a single image/text across spaces is one logical entry), and
    eviction drops the least-recently-used content key entirely.
    """

    def __init__(self, *, max_entries: int = 0) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, dict[str, EmbeddingVector]] = OrderedDict()
        self.stats = CacheStats()

    async def get(self, key: str, space: VectorSpace) -> EmbeddingVector | None:
        bucket = self._store.get(key)
        if bucket is None:
            self.stats.misses += 1
            return None
        vec = bucket.get(space.key)
        if vec is None:
            self.stats.misses += 1
            return None
        self._store.move_to_end(key)  # mark MRU
        self.stats.hits += 1
        return vec

    async def put(self, key: str, vector: EmbeddingVector) -> None:
        bucket = self._store.get(key)
        if bucket is None:
            bucket = {}
            self._store[key] = bucket
        bucket[vector.space.key] = vector
        self._store.move_to_end(key)
        self.stats.stores += 1
        self._evict_if_needed()

    async def keys(self) -> list[str]:
        return list(self._store.keys())

    async def spaces_for(self, key: str) -> list[VectorSpace]:
        bucket = self._store.get(key, {})
        return [v.space for v in bucket.values()]

    async def invalidate_space(self, space: VectorSpace) -> int:
        """Drop every cached vector in ``space`` (returns count removed)."""
        removed = 0
        empty: list[str] = []
        for key, bucket in self._store.items():
            if bucket.pop(space.key, None) is not None:
                removed += 1
            if not bucket:
                empty.append(key)
        for key in empty:
            self._store.pop(key, None)
        return removed

    def _evict_if_needed(self) -> None:
        if self._max <= 0:
            return
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # drop LRU
            self.stats.evictions += 1


async def reembed_stale(
    cache: InMemoryEmbeddingCache,
    *,
    target: Embedder,
    resolve_source: Callable[[str], Awaitable[bytes | str | None]],
) -> ReembedReport:
    """Re-embed every cached content key that lacks a vector in ``target.space``.

    ``resolve_source(content_key)`` returns the *original* payload (image bytes
    or text) so it can be recomputed, or ``None`` if the source is gone (counted
    as a failure, not an exception). The modality is read from the content key's
    prefix. New vectors are stored alongside the old ones (the old space's
    vectors are left for a separate ``invalidate_space`` if desired), so the
    migration is non-destructive and re-runnable.
    """
    target_space = target.space
    report = ReembedReport(target_space_key=target_space.key)
    keys = await cache.keys()
    for key in keys:
        examined = report.examined + 1
        spaces = {s.key for s in await cache.spaces_for(key)}
        if target_space.key in spaces:
            report = _bump(report, examined=examined, skipped_current=report.skipped_current + 1)
            continue
        source = await resolve_source(key)
        if source is None:
            report = _bump(
                report,
                examined=examined,
                failed=report.failed + 1,
                failed_ids=[*report.failed_ids, key],
            )
            continue
        modality = Modality(key.split(":", 1)[0])
        if modality is Modality.IMAGE:
            payload = source if isinstance(source, bytes) else str(source).encode("utf-8")
            vectors = await target.embed_images([payload])
        else:
            text = source.decode("utf-8") if isinstance(source, bytes) else str(source)
            vectors = await target.embed_texts([text])
        await cache.put(key, vectors[0])
        report = _bump(report, examined=examined, reembedded=report.reembedded + 1)
    return report


def _bump(
    report: ReembedReport,
    *,
    examined: int | None = None,
    reembedded: int | None = None,
    skipped_current: int | None = None,
    failed: int | None = None,
    failed_ids: Sequence[str] | None = None,
) -> ReembedReport:
    return ReembedReport(
        target_space_key=report.target_space_key,
        examined=examined if examined is not None else report.examined,
        reembedded=reembedded if reembedded is not None else report.reembedded,
        skipped_current=skipped_current if skipped_current is not None else report.skipped_current,
        failed=failed if failed is not None else report.failed,
        failed_ids=list(failed_ids) if failed_ids is not None else report.failed_ids,
    )


__all__ = [
    "CacheStats",
    "EmbeddingCache",
    "InMemoryEmbeddingCache",
    "content_key",
    "content_key_for_image",
    "content_key_for_text",
    "reembed_stale",
]
