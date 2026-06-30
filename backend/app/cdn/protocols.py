"""Injectable seams the CDN layer depends on (so everything is testable offline).

Three protocols, all async, kept deliberately minimal:

* :class:`RegionStore` — one object store bound to a single region. A thin async
  shape over the operations :class:`app.storage.object_store.ObjectStore`
  already provides (``put_bytes``/``get_bytes``/``exists``/``delete`` +
  ``presigned_get_url``/``public_url``). The real boto3 client is synchronous;
  an adapter wrapping it in a thread satisfies this protocol without touching
  the existing object-store code. Tests inject an in-memory fake.

* :class:`CdnProvider` — the pluggable edge in front of a region: invalidate
  (purge) a key, and warm/prefetch a key into the edge cache ahead of playback.
  Abstracts CloudFront / Alibaba DCDN / Fastly differences behind two calls.

* :class:`Clock` — monotonic-ish wall clock seam so replication-lag and cache
  TTL maths are deterministic in tests (inject a fake clock).

No concrete implementation here couples to boto3 — the adapter lives in
:mod:`app.cdn.adapters`, imported lazily by the composition root.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RegionStore(Protocol):
    """One async object store bound to a single region's bucket."""

    @property
    def region_id(self) -> str:
        """The region this store serves."""
        ...

    async def put_bytes(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        """Upload raw bytes to ``key`` (overwrites)."""
        ...

    async def get_bytes(self, key: str) -> bytes:
        """Download the object at ``key`` as bytes (raises if absent)."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""
        ...

    async def delete(self, key: str) -> None:
        """Delete ``key`` (no error if already absent)."""
        ...

    async def size(self, key: str) -> int | None:
        """Byte length of ``key``, or ``None`` if absent."""
        ...

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        """A time-limited signed GET URL for ``key``."""
        ...

    def public_url(self, key: str) -> str | None:
        """A stable public URL for ``key`` if a public base is configured."""
        ...


@runtime_checkable
class CdnProvider(Protocol):
    """The pluggable edge cache in front of a region."""

    @property
    def region_id(self) -> str:
        """The region this edge fronts."""
        ...

    async def invalidate(self, key: str) -> None:
        """Purge ``key`` from the edge cache (purge-on-invalidate)."""
        ...

    async def warm(self, key: str, origin_url: str) -> None:
        """Prefetch ``key`` into the edge cache from ``origin_url`` ahead of demand."""
        ...

    async def is_cached(self, key: str) -> bool:
        """Whether ``key`` is currently warm in the edge (for prefetch dedup)."""
        ...


@runtime_checkable
class Clock(Protocol):
    """A wall-clock seam (epoch seconds) for deterministic time-based logic."""

    def now(self) -> float:
        """Current time as epoch seconds."""
        ...


__all__ = ["CdnProvider", "Clock", "RegionStore"]
