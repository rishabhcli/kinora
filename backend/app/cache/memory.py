"""L1 — the in-process LRU + TTL backend.

Holds live Python objects (no serialization) in an insertion-ordered dict used
as an LRU: a read/write moves the key to the most-recently-used end, and when
``max_entries`` is exceeded the least-recently-used key is evicted. TTL is
absolute (``entry.expires_at``); an expired entry reads as a miss and is
lazily purged. A reverse tag index lets :meth:`delete_tag` drop a whole tag in
one pass.

This backend is the whole story in "in-memory-only mode": a :class:`MemoryCache`
plus the :class:`~app.cache.cache.Cache` facade needs **no infra at all**, which
is what keeps the cache layer unit-testable and lets the app degrade to a purely
local cache when Redis is unavailable.

Eviction emits a count through an injected :class:`~app.cache.metrics.CacheMetrics`
so the namespace's ``evictions`` counter reflects pressure. The structure is
guarded by an ``asyncio.Lock`` because trim/expiry mutate shared state across
awaits in the facade.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable

from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.entry import CacheEntry
from app.cache.interface import CacheBackend
from app.cache.metrics import CacheMetrics


class MemoryCache(CacheBackend):
    """An LRU + TTL in-process cache backend.

    Args:
        max_entries: Hard cap on stored keys; 0 disables the cap (unbounded).
        clock: Time source (defaults to the real clock).
        metrics: Optional shared metrics bag; eviction/expiry are reported under
            ``metrics_namespace``.
        metrics_namespace: Namespace label used for this backend's metric bumps.
    """

    name = "memory"

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        clock: Clock | None = None,
        metrics: CacheMetrics | None = None,
        metrics_namespace: str = "memory",
    ) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")
        self._max = max_entries
        self._clock = clock or SYSTEM_CLOCK
        self._metrics = metrics
        self._ns = metrics_namespace
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        # tag -> set of keys carrying that tag (reverse index for delete_tag).
        self._tags: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # --- internals --- #

    def _index_tags(self, key: str, entry: CacheEntry) -> None:
        for tag in entry.tags:
            self._tags.setdefault(tag, set()).add(key)

    def _deindex_tags(self, key: str, entry: CacheEntry) -> None:
        for tag in entry.tags:
            keys = self._tags.get(tag)
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del self._tags[tag]

    def _evict_lru(self) -> None:
        # Drop oldest until within cap. max==0 means unbounded.
        if self._max == 0:
            return
        while len(self._store) > self._max:
            key, entry = self._store.popitem(last=False)
            self._deindex_tags(key, entry)
            if self._metrics is not None:
                self._metrics.inc_eviction(self._ns)

    def _purge_if_expired(self, key: str, entry: CacheEntry, now: float) -> bool:
        if entry.is_expired(now):
            self._store.pop(key, None)
            self._deindex_tags(key, entry)
            if self._metrics is not None:
                self._metrics.inc_expiration(self._ns)
            return True
        return False

    # --- CacheBackend --- #

    async def get(self, key: str) -> CacheEntry | None:
        now = self._clock.time()
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if self._purge_if_expired(key, entry, now):
                return None
            self._store.move_to_end(key)  # mark MRU
            return entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._lock:
            old = self._store.get(key)
            if old is not None:
                self._deindex_tags(key, old)
            self._store[key] = entry
            self._store.move_to_end(key)
            self._index_tags(key, entry)
            self._evict_lru()

    async def delete(self, key: str) -> bool:
        async with self._lock:
            entry = self._store.pop(key, None)
            if entry is None:
                return False
            self._deindex_tags(key, entry)
            return True

    async def delete_many(self, keys: Iterable[str]) -> int:
        async with self._lock:
            removed = 0
            for key in keys:
                entry = self._store.pop(key, None)
                if entry is not None:
                    self._deindex_tags(key, entry)
                    removed += 1
            return removed

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
            self._tags.clear()

    async def delete_tag(self, tag: str) -> int:
        async with self._lock:
            keys = self._tags.pop(tag, set())
            removed = 0
            for key in list(keys):
                entry = self._store.pop(key, None)
                if entry is not None:
                    # Remove this key from its *other* tags' indexes too.
                    for other in entry.tags:
                        if other == tag:
                            continue
                        bucket = self._tags.get(other)
                        if bucket is not None:
                            bucket.discard(key)
                            if not bucket:
                                del self._tags[other]
                    removed += 1
            return removed

    async def health(self) -> bool:
        return True

    # --- introspection (useful in tests / dashboards) --- #

    def size(self) -> int:
        """Current number of stored keys (may include not-yet-purged expired ones)."""
        return len(self._store)

    def keys(self) -> list[str]:
        """Snapshot of stored keys, LRU order (oldest first)."""
        return list(self._store.keys())

    def tags(self) -> list[str]:
        """Snapshot of tags with at least one live key."""
        return list(self._tags.keys())


__all__ = ["MemoryCache"]
