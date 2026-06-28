"""A versioned flag-snapshot cache with streaming (pub/sub) invalidation.

Evaluating a flag must be a hot, in-process, zero-I/O operation, but the flag
*definitions* live in Postgres and change out from under every process when an
operator edits a flag. The cache bridges that: each process holds the current
:class:`~app.flags.models.FlagSnapshot` in memory and serves evaluations from it
directly; a Redis pub/sub channel broadcasts "flags changed" so every process
refetches *once* and swaps its snapshot atomically — instead of every evaluation
hitting the database or polling.

Design:

* **Local hold + TTL.** The snapshot is held in memory and considered fresh for
  ``ttl_s``; after that the next access triggers a background-safe reload. The
  TTL is a safety net — the primary freshness mechanism is the stream.
* **Streaming invalidation.** :meth:`publish_invalidation` bumps a Redis version
  counter and publishes on the channel; :meth:`listen` (an async iterator) yields
  whenever an invalidation arrives so the service can reload immediately.
* **Fail-open.** If Redis is unreachable the cache degrades to TTL-only polling;
  evaluation never blocks on the cache being warm (it serves the last known
  snapshot, or the empty snapshot on a cold start).

The cache is loader-driven: it is handed an async ``loader`` (normally
``FlagStore.load_snapshot``) and owns *when* to call it, never *how* flags are
stored. That keeps it independent of the persistence layer and unit-testable
with a trivial in-memory loader and no Redis.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable

from app.core.logging import get_logger
from app.flags.models import EMPTY_SNAPSHOT, FlagSnapshot
from app.redis.client import RedisClient

logger = get_logger("app.flags.cache")

SnapshotLoader = Callable[[], Awaitable[FlagSnapshot]]

#: Default pub/sub channel + version key (overridable via Settings).
DEFAULT_CHANNEL = "kinora:flags:invalidate"
_VERSION_KEY = "kinora:flags:version"


class FlagCache:
    """In-memory snapshot cache with optional Redis-streamed invalidation."""

    def __init__(
        self,
        loader: SnapshotLoader,
        *,
        redis: RedisClient | None = None,
        ttl_s: float = 30.0,
        channel: str = DEFAULT_CHANNEL,
    ) -> None:
        self._loader = loader
        self._redis = redis
        self._ttl_s = ttl_s
        self._channel = channel
        self._snapshot: FlagSnapshot = EMPTY_SNAPSHOT
        self._loaded_at: float = 0.0
        self._warm = False

    @property
    def channel(self) -> str:
        """The pub/sub channel invalidations are broadcast on."""
        return self._channel

    @property
    def current(self) -> FlagSnapshot:
        """The last-loaded snapshot without triggering a reload (may be stale)."""
        return self._snapshot

    @property
    def is_stale(self) -> bool:
        """Whether the held snapshot has aged past its TTL."""
        return (not self._warm) or (time.monotonic() - self._loaded_at) >= self._ttl_s

    async def get(self, *, force: bool = False) -> FlagSnapshot:
        """Return a fresh snapshot, reloading from the loader if stale or forced."""
        if force or self.is_stale:
            await self.reload()
        return self._snapshot

    async def reload(self) -> FlagSnapshot:
        """Load a new snapshot and swap it in atomically. Fails open on loader error."""
        try:
            snapshot = await self._loader()
        except Exception as exc:  # noqa: BLE001 - never let a load failure break eval
            logger.warning("flags.cache.reload_failed", error=str(exc))
            # Keep serving the last known snapshot; mark warm so we don't hot-loop.
            self._warm = True
            self._loaded_at = time.monotonic()
            return self._snapshot
        self._snapshot = snapshot
        self._loaded_at = time.monotonic()
        self._warm = True
        logger.debug(
            "flags.cache.reloaded", version=snapshot.version, flags=len(snapshot.flags)
        )
        return snapshot

    async def publish_invalidation(self) -> int:
        """Bump the shared version and broadcast on the channel.

        Returns the number of subscribers notified (0 when Redis is absent). Call
        this after every durable flag write so all processes refetch promptly.
        """
        if self._redis is None:
            return 0
        try:
            version = await self._redis.raw.incr(_VERSION_KEY)
            return await self._redis.publish(self._channel, {"version": int(version)})
        except Exception as exc:  # noqa: BLE001 - invalidation is best-effort
            logger.warning("flags.cache.publish_failed", error=str(exc))
            return 0

    async def remote_version(self) -> int | None:
        """The shared invalidation version (``None`` if Redis is absent/unreachable)."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.raw.get(_VERSION_KEY)
            return int(raw) if raw is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("flags.cache.version_failed", error=str(exc))
            return None

    async def listen(self, *, timeout: float = 5.0) -> AsyncIterator[int]:
        """Yield the new version whenever an invalidation is published.

        A long-running consumer (the service's background refresher) iterates
        this and calls :meth:`reload` on each yield. No-op (immediately returns)
        when Redis is absent so callers can ``async for`` unconditionally.
        """
        if self._redis is None:
            return
        async with self._redis.subscribe(self._channel) as pubsub:
            while True:
                message = await self._redis.next_message(pubsub, timeout=timeout)
                if message is None:
                    continue
                version = message.get("version") if isinstance(message, dict) else None
                yield int(version) if version is not None else self._snapshot.version


__all__ = ["DEFAULT_CHANNEL", "FlagCache", "SnapshotLoader"]
