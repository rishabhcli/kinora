"""Factories — build caches without hand-wiring backends at every call site.

Three entry points:

* :func:`memory_cache` — a pure in-process cache (no infra). The default for
  tests and for any path that just wants local memoization.
* :func:`tiered_cache` — L1 (in-process) in front of an L2 Redis backend, sharing
  one metrics bag. Used when a value should survive a single process and be
  shared across workers.
* :class:`CacheManager` — owns a shared :class:`~app.cache.metrics.CacheMetrics`
  and a shared :class:`~app.cache.clock.Clock`, and hands out per-namespace
  :class:`~app.cache.cache.Cache` instances (memoizing them by namespace). This
  is what a composition root would hold one of.

Nothing here imports application settings directly; callers pass a Redis handle /
URL explicitly, so the cache layer stays decoupled from ``app.core.config`` and
the wiring stays additive.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from app.cache.cache import Cache, CacheConfig
from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.codecs import DEFAULT_CODEC, Codec
from app.cache.interface import CacheBackend
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics
from app.cache.null import NullCache
from app.cache.redis_backend import RedisCache
from app.cache.tiered import TieredCache


def memory_cache(
    *,
    namespace: str = "default",
    max_entries: int = 1024,
    config: CacheConfig | None = None,
    clock: Clock | None = None,
    metrics: CacheMetrics | None = None,
    rng: random.Random | None = None,
) -> Cache[Any]:
    """A pure in-process cache (the no-infra mode)."""
    clk = clock or SYSTEM_CLOCK
    mx = metrics or CacheMetrics()
    backend: CacheBackend = MemoryCache(
        max_entries=max_entries,
        clock=clk,
        metrics=mx,
        metrics_namespace=namespace,
    )
    return Cache(backend, namespace=namespace, config=config, clock=clk, metrics=mx, rng=rng)


def redis_cache(
    redis: Any,
    *,
    namespace: str = "default",
    prefix: str = "kinora:cache",
    codec: Codec | None = None,
    config: CacheConfig | None = None,
    clock: Clock | None = None,
    metrics: CacheMetrics | None = None,
    rng: random.Random | None = None,
    lock_factory: Callable[[str], Any] | None = None,
) -> Cache[Any]:
    """A Redis-only cache (no L1). ``redis`` must be a binary-mode async client."""
    clk = clock or SYSTEM_CLOCK
    mx = metrics or CacheMetrics()
    backend: CacheBackend = RedisCache(
        redis, prefix=f"{prefix}:{namespace}", codec=codec or DEFAULT_CODEC, clock=clk
    )
    return Cache(
        backend,
        namespace=namespace,
        config=config,
        clock=clk,
        metrics=mx,
        rng=rng,
        lock_factory=lock_factory,
    )


def tiered_cache(
    redis: Any,
    *,
    namespace: str = "default",
    prefix: str = "kinora:cache",
    l1_max_entries: int = 1024,
    codec: Codec | None = None,
    config: CacheConfig | None = None,
    clock: Clock | None = None,
    metrics: CacheMetrics | None = None,
    rng: random.Random | None = None,
    lock_factory: Callable[[str], Any] | None = None,
    l2_fail_open: bool = True,
) -> Cache[Any]:
    """An L1 (memory) + L2 (Redis) cache sharing one metrics bag."""
    clk = clock or SYSTEM_CLOCK
    mx = metrics or CacheMetrics()
    l1 = MemoryCache(
        max_entries=l1_max_entries, clock=clk, metrics=mx, metrics_namespace=namespace
    )
    l2 = RedisCache(
        redis, prefix=f"{prefix}:{namespace}", codec=codec or DEFAULT_CODEC, clock=clk
    )
    backend: CacheBackend = TieredCache(
        l1,
        l2,
        clock=clk,
        metrics=mx,
        metrics_namespace=namespace,
        l2_fail_open=l2_fail_open,
    )
    return Cache(
        backend,
        namespace=namespace,
        config=config,
        clock=clk,
        metrics=mx,
        rng=rng,
        lock_factory=lock_factory,
    )


class CacheManager:
    """Hands out per-namespace caches sharing one metrics bag and clock.

    A composition root holds one :class:`CacheManager`. The first time a namespace
    is requested it is built (memory-only, or tiered when a Redis handle was
    provided) and memoized; subsequent requests return the same instance.
    """

    def __init__(
        self,
        *,
        redis: Any = None,
        prefix: str = "kinora:cache",
        clock: Clock | None = None,
        rng: random.Random | None = None,
        default_l1_max_entries: int = 1024,
        lock_factory: Callable[[str], Any] | None = None,
        l2_fail_open: bool = True,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._clock = clock or SYSTEM_CLOCK
        self._rng = rng or random.Random()
        self._metrics = CacheMetrics()
        self._default_l1 = default_l1_max_entries
        self._lock_factory = lock_factory
        self._l2_fail_open = l2_fail_open
        self._caches: dict[str, Cache[Any]] = {}

    @property
    def metrics(self) -> CacheMetrics:
        return self._metrics

    @property
    def has_redis(self) -> bool:
        return self._redis is not None

    def get(
        self,
        namespace: str,
        *,
        config: CacheConfig | None = None,
        max_entries: int | None = None,
        codec: Codec | None = None,
    ) -> Cache[Any]:
        """Return (building once) the cache for ``namespace``."""
        existing = self._caches.get(namespace)
        if existing is not None:
            return existing
        l1_max = max_entries if max_entries is not None else self._default_l1
        if self._redis is None:
            cache = memory_cache(
                namespace=namespace,
                max_entries=l1_max,
                config=config,
                clock=self._clock,
                metrics=self._metrics,
                rng=self._rng,
            )
        else:
            cache = tiered_cache(
                self._redis,
                namespace=namespace,
                prefix=self._prefix,
                l1_max_entries=l1_max,
                codec=codec,
                config=config,
                clock=self._clock,
                metrics=self._metrics,
                rng=self._rng,
                lock_factory=self._lock_factory,
                l2_fail_open=self._l2_fail_open,
            )
        self._caches[namespace] = cache
        return cache

    def namespaces(self) -> list[str]:
        """Namespaces built so far."""
        return list(self._caches.keys())

    def snapshot(self) -> dict[str, Any]:
        """Per-namespace metrics for a dashboard / metrics endpoint."""
        return {ns: stats.as_dict() for ns, stats in self._metrics.snapshot().items()}

    async def close(self) -> None:
        """Close every built cache's backend."""
        for cache in self._caches.values():
            await cache.close()


def null_cache(*, namespace: str = "default") -> Cache[Any]:
    """A cache that never stores anything (caching disabled)."""
    return Cache(NullCache(), namespace=namespace)


__all__ = [
    "CacheManager",
    "memory_cache",
    "null_cache",
    "redis_cache",
    "tiered_cache",
]
