"""A no-op cache backend — every read misses, every write is dropped.

Useful as the L2 placeholder when Redis is not configured (so a tiered cache
can be built unconditionally and simply behaves as L1-only), and as a way to
disable caching globally without changing call sites.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.cache.entry import CacheEntry
from app.cache.interface import CacheBackend


class NullCache(CacheBackend):
    """A backend that stores nothing."""

    name = "null"

    async def get(self, key: str) -> CacheEntry | None:
        return None

    async def set(self, key: str, entry: CacheEntry) -> None:
        return None

    async def delete(self, key: str) -> bool:
        return False

    async def delete_many(self, keys: Iterable[str]) -> int:
        return 0

    async def clear(self) -> None:
        return None

    async def delete_tag(self, tag: str) -> int:
        return 0

    async def health(self) -> bool:
        return True


__all__ = ["NullCache"]
