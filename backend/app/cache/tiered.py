"""Tiered backend — L1 (fast, local) in front of L2 (shared, durable).

A read checks L1 first; on an L1 miss it falls through to L2 and, on an L2 hit,
**promotes** the entry back into L1 (so the next local read is fast). A write
goes to both tiers (write-through). Deletes and tag-deletes fan out to both.

This is a :class:`~app.cache.interface.CacheBackend` itself, so the
:class:`~app.cache.cache.Cache` facade treats a two-tier setup identically to a
single backend. Promotion respects the entry's remaining TTL, never extending it
past L2's view of expiry.

L2 errors are *soft* when ``l2_fail_open`` is set: a Redis blip degrades the
tiered cache to L1-only for that operation instead of raising. The error is
still counted so the degradation is observable.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.interface import CacheBackend
from app.cache.metrics import CacheMetrics


class TieredCache(CacheBackend):
    """Compose an L1 and an L2 backend into a single read-through/write-through cache."""

    name = "tiered"

    def __init__(
        self,
        l1: CacheBackend,
        l2: CacheBackend,
        *,
        clock: Clock | None = None,
        metrics: CacheMetrics | None = None,
        metrics_namespace: str = "tiered",
        l2_fail_open: bool = True,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._clock = clock or SYSTEM_CLOCK
        self._metrics = metrics
        self._ns = metrics_namespace
        self._fail_open = l2_fail_open

    @property
    def l1(self) -> CacheBackend:
        return self._l1

    @property
    def l2(self) -> CacheBackend:
        return self._l2

    async def get(self, key: str) -> CacheEntry | None:
        entry = await self._l1.get(key)
        if entry is not None:
            if self._metrics is not None:
                self._metrics.inc_l1_hit(self._ns)
            return entry
        try:
            entry = await self._l2.get(key)
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if self._fail_open:
                return None
            raise
        if entry is None:
            return None
        if self._metrics is not None:
            self._metrics.inc_l2_hit(self._ns)
        await self._promote(key, entry)
        return entry

    async def _promote(self, key: str, entry: CacheEntry) -> None:
        """Re-seat an L2 hit into L1, honouring its remaining TTL."""
        if entry.expires_at is not None and entry.is_expired(self._clock.time()):
            return
        await self._l1.set(key, entry)

    async def set(self, key: str, entry: CacheEntry) -> None:
        await self._l1.set(key, entry)
        try:
            await self._l2.set(key, entry)
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if not self._fail_open:
                raise

    async def delete(self, key: str) -> bool:
        l1_removed = await self._l1.delete(key)
        l2_removed = False
        try:
            l2_removed = await self._l2.delete(key)
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if not self._fail_open:
                raise
        return l1_removed or l2_removed

    async def delete_many(self, keys: Iterable[str]) -> int:
        keys = list(keys)
        n1 = await self._l1.delete_many(keys)
        n2 = 0
        try:
            n2 = await self._l2.delete_many(keys)
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if not self._fail_open:
                raise
        return max(n1, n2)

    async def clear(self) -> None:
        await self._l1.clear()
        try:
            await self._l2.clear()
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if not self._fail_open:
                raise

    async def delete_tag(self, tag: str) -> int:
        n1 = await self._l1.delete_tag(tag)
        n2 = 0
        try:
            n2 = await self._l2.delete_tag(tag)
        except CacheBackendError:
            if self._metrics is not None:
                self._metrics.inc_backend_error(self._ns)
            if not self._fail_open:
                raise
        return max(n1, n2)

    async def health(self) -> bool:
        # Healthy if L1 is up; L2 is best-effort under fail-open.
        l1_ok = await self._l1.health()
        if not self._fail_open:
            return l1_ok and await self._l2.health()
        return l1_ok

    async def close(self) -> None:
        await self._l1.close()
        await self._l2.close()


__all__ = ["TieredCache"]
