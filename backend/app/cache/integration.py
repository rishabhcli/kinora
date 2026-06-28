"""Composition helpers — build a :class:`CacheManager` from settings.

Kept in this package (rather than edited into ``app.composition``) so the cache
layer wires itself additively: a composition root that wants a shared cache calls
:func:`build_cache_manager` and stores the result, with no changes to the
:class:`~app.composition.Container` dataclass required.

The L2 (Redis) tier needs a **binary-mode** client (``decode_responses=False``)
because codec payloads can be raw bytes (pickle), whereas the app's shared
:class:`~app.redis.client.RedisClient` is text-mode. :func:`binary_redis_from_url`
builds the right client; passing ``redis=None`` (or a settings object whose
``redis_url`` is empty) yields a pure in-memory manager that needs no infra — the
safe default for tests and for any environment without Redis.

A small cross-process lock factory bridges the cache facade's optional
``lock_factory`` to the existing :class:`~app.redis.client.DistributedLock`, so
read-through loads can be single-flighted across workers when a text-mode
:class:`RedisClient` is available.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.cache.factory import CacheManager


def binary_redis_from_url(url: str) -> Any:
    """A binary-mode async Redis client suitable for the L2 cache tier."""
    from redis.asyncio import Redis

    return Redis.from_url(url, decode_responses=False)


def redis_lock_factory(redis_client: Any, *, ttl_ms: int = 10_000) -> Callable[[str], Any]:
    """Adapt a text-mode :class:`~app.redis.client.RedisClient` to a lock factory.

    Returns ``name -> async-context-manager`` using the client's ``.lock(...)``.
    Non-blocking-ish: a short blocking timeout so a contended load falls back to
    computing locally rather than stalling (the facade catches lock failures and
    proceeds with a plain load).
    """

    def factory(name: str) -> Any:
        return redis_client.lock(name, ttl_ms=ttl_ms, blocking=True, blocking_timeout=2.0)

    return factory


def build_cache_manager(
    *,
    redis_url: str | None = None,
    lock_redis: Any = None,
    prefix: str = "kinora:cache",
    default_l1_max_entries: int = 1024,
    l2_fail_open: bool = True,
) -> CacheManager:
    """Build a :class:`CacheManager`.

    Args:
        redis_url: When set (and non-empty), an L2 Redis tier is enabled using a
            fresh binary-mode client built from this URL. When ``None``/empty the
            manager is pure in-memory (no infra).
        lock_redis: Optional text-mode :class:`~app.redis.client.RedisClient` used
            to enable cross-process single-flight on read-through loads.
        prefix: Redis key prefix scoping all cache keys.
        default_l1_max_entries: Default per-namespace L1 capacity.
        l2_fail_open: When True a Redis blip degrades to L1-only instead of raising.
    """
    redis = binary_redis_from_url(redis_url) if redis_url else None
    lock_factory = redis_lock_factory(lock_redis) if lock_redis is not None else None
    return CacheManager(
        redis=redis,
        prefix=prefix,
        default_l1_max_entries=default_l1_max_entries,
        lock_factory=lock_factory,
        l2_fail_open=l2_fail_open,
    )


def build_cache_manager_from_settings(settings: Any, *, lock_redis: Any = None) -> CacheManager:
    """Build a :class:`CacheManager` from an app ``Settings`` object.

    Reads ``settings.redis_url`` for the L2 tier. Falls back to in-memory mode if
    the attribute is missing or empty, so it is safe to call with the lazy
    no-network settings used in tests.
    """
    redis_url = getattr(settings, "redis_url", None)
    return build_cache_manager(redis_url=redis_url, lock_redis=lock_redis)


__all__ = [
    "binary_redis_from_url",
    "build_cache_manager",
    "build_cache_manager_from_settings",
    "redis_lock_factory",
]
