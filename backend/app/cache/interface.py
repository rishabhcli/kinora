"""The cache backend interface — what L1, L2, null, and tiered all implement.

A :class:`CacheBackend` is the low-level storage seam: it stores and returns
:class:`~app.cache.entry.CacheEntry` objects keyed by an already-fully-qualified
string key (namespacing/derivation happens above, in the
:class:`~app.cache.cache.Cache` facade). Backends own only:

* ``get`` / ``set`` / ``delete`` / ``clear`` of entries,
* TTL enforcement (an expired entry must read as a miss),
* optional tag tracking so :meth:`delete_tag` can drop every entry in a tag, and
* a ``health`` probe.

Everything richer — cache-aside, single-flight, negative caching, early
expiry, metrics — is layered on top so it works identically over any backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from app.cache.entry import CacheEntry


class CacheBackend(ABC):
    """Abstract async storage for :class:`CacheEntry` objects."""

    #: Stable backend name for metrics/diagnostics (e.g. "memory", "redis").
    name: str = "backend"

    @abstractmethod
    async def get(self, key: str) -> CacheEntry | None:
        """Return the live entry for ``key`` or ``None`` (miss / expired)."""

    @abstractmethod
    async def set(self, key: str, entry: CacheEntry) -> None:
        """Store ``entry`` at ``key``, honouring its ``expires_at`` as a TTL."""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Drop ``key``; returns whether something was removed."""

    @abstractmethod
    async def delete_many(self, keys: Iterable[str]) -> int:
        """Drop several keys; returns the count removed."""

    @abstractmethod
    async def clear(self) -> None:
        """Drop everything this backend owns (namespace-scoped if applicable)."""

    @abstractmethod
    async def delete_tag(self, tag: str) -> int:
        """Drop every entry tagged ``tag``; returns the count removed."""

    @abstractmethod
    async def health(self) -> bool:
        """Cheap liveness probe; ``True`` when the backend is reachable."""

    async def get_many(self, keys: Iterable[str]) -> dict[str, CacheEntry]:
        """Batch get; default loops :meth:`get` (backends may override)."""
        out: dict[str, CacheEntry] = {}
        for key in keys:
            entry = await self.get(key)
            if entry is not None:
                out[key] = entry
        return out

    async def set_many(self, items: dict[str, CacheEntry]) -> None:
        """Batch set; default loops :meth:`set` (backends may override)."""
        for key, entry in items.items():
            await self.set(key, entry)

    async def close(self) -> None:
        """Release any resources (no-op by default)."""
        return None


__all__ = ["CacheBackend"]
