"""Content-hash memoization of deterministic results — net-new (nothing memoizes these today).

The only content-hash cache in the codebase is the ``shot_cache`` clip cache. Canon queries, page
analysis, and deterministic agent outputs are recomputed (re-embedded / re-prompted) every time.
:class:`ResultCache` wraps a Redis-like backend (``get_json`` / ``set_json`` with ``ttl_s`` — the
``RedisClient`` surface) so a re-query of identical content is served without re-calling the model
— the measurable token/call saving on re-ingest and re-open.

**Use only for deterministic, content-addressed computations.** The key must capture every input
that can change the output (e.g. ``canon_version``), so a cache hit can never be stale. Disabled
(the wiring default) it is a pass-through. Cache read/write failures never break the wrapped call.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from app.core.logging import get_logger

logger = get_logger("app.optim.cache")

T = TypeVar("T")

#: Unit-separator join (cannot appear in id text) — matches db/hashing.py's convention.
_SEP = "\x1f"
_PREFIX = "kinora:optim:cache"


def content_hash(*parts: object) -> str:
    """Stable, order-sensitive sha256 hex over ``parts`` (``str()``-ified, ``\\x1f``-joined)."""
    payload = _SEP.join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_key(namespace: str, *parts: object) -> str:
    """Namespaced Redis key: ``kinora:optim:cache:<namespace>:<content_hash(parts)>``."""
    return f"{_PREFIX}:{namespace}:{content_hash(*parts)}"


class CacheBackend(Protocol):
    """The slice of ``RedisClient`` the cache needs (so tests use an in-memory fake)."""

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None: ...


@dataclass
class CacheStats:
    """Hit/miss tally (for the ``/api/optim`` rollup and PERF measurement)."""

    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


def _identity(value: Any) -> Any:
    return value


class ResultCache:
    """Memoize an async ``factory`` by a content-hash key, through a Redis-like backend."""

    def __init__(
        self,
        backend: CacheBackend,
        *,
        namespace: str,
        ttl_s: int | None = 3600,
        enabled: bool = True,
        serialize: Callable[[Any], Any] = _identity,
        deserialize: Callable[[Any], Any] = _identity,
    ) -> None:
        self.backend = backend
        self.namespace = namespace
        self.ttl_s = ttl_s
        self.enabled = enabled
        self._serialize = serialize
        self._deserialize = deserialize
        self.stats = CacheStats()

    async def get_or_compute(
        self, key_parts: Sequence[object], factory: Callable[[], Awaitable[T]]
    ) -> T:
        """Return the cached value for ``key_parts``, else compute + store (best-effort) + return.

        ``None`` results are cached (in a ``{"v": ...}`` envelope) so they don't read back as a
        miss. Disabled ⇒ always compute. Backend errors are swallowed (logged); the call returns.
        """
        if not self.enabled:
            return await factory()
        key = cache_key(self.namespace, *key_parts)
        envelope: Any | None = None
        try:
            envelope = await self.backend.get_json(key)
        except Exception:
            logger.warning("cache.read_failed", namespace=self.namespace)
        if isinstance(envelope, dict) and "v" in envelope:
            self.stats.hits += 1
            return self._deserialize(envelope["v"])
        self.stats.misses += 1
        result = await factory()
        try:
            await self.backend.set_json(key, {"v": self._serialize(result)}, ttl_s=self.ttl_s)
        except Exception:
            logger.warning("cache.write_failed", namespace=self.namespace)
        return result


__all__ = ["CacheBackend", "CacheStats", "ResultCache", "cache_key", "content_hash"]
