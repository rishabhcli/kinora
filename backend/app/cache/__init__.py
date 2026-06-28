"""Unified multi-tier caching subsystem for the Kinora backend.

A general application cache — distinct from the shot-hash-specific
:mod:`app.memory.cache_service` (the §8.7 render dedup cache). This package gives
the rest of the backend a typed, namespaced cache with:

* **Two tiers** — an in-process LRU/TTL L1 (:class:`MemoryCache`) and a Redis L2
  (:class:`RedisCache`), composed by :class:`TieredCache`; an
  in-memory-only mode needs no infra at all.
* **Cache-aside / read-through / write-through** helpers on the :class:`Cache`
  facade, plus a :func:`cached` decorator for memoizing async functions.
* **Stampede protection** — in-process single-flight, an optional cross-process
  Redis lock, and probabilistic early expiry (XFetch).
* **Negative caching** — repeated lookups of an absent key stay cheap.
* **Tag- and key-based invalidation** — the §8.7 "change one character, only the
  shots that reference it re-render" pattern, generalised.
* **Per-namespace metrics** (:class:`CacheMetrics`) and a deterministic
  :class:`FakeClock` test harness.

Typical use::

    from app.cache import CacheManager, CacheConfig

    manager = CacheManager(redis=redis_binary_client)        # or no redis= for L1-only
    cache = manager.get("canon-embed", config=CacheConfig(default_ttl=600))
    value = await cache.get_or_load(key, loader, tags=["entity:42"])
    await cache.invalidate_tag("entity:42")
"""

from __future__ import annotations

from app.cache.cache import Cache, CacheConfig, tag_key_for
from app.cache.clock import SYSTEM_CLOCK, Clock, FakeClock, SystemClock
from app.cache.codecs import BytesCodec, Codec, JsonCodec, PickleCodec
from app.cache.decorator import CachedFunction, cached
from app.cache.entry import CacheEntry
from app.cache.errors import (
    CacheBackendError,
    CacheError,
    SerializationError,
    SingleFlightError,
)
from app.cache.factory import (
    CacheManager,
    memory_cache,
    null_cache,
    redis_cache,
    tiered_cache,
)
from app.cache.integration import (
    binary_redis_from_url,
    build_cache_manager,
    build_cache_manager_from_settings,
    redis_lock_factory,
)
from app.cache.interface import CacheBackend
from app.cache.keys import derive_key, fingerprint, qualify
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics, NamespaceStats
from app.cache.null import NullCache
from app.cache.redis_backend import RedisCache
from app.cache.singleflight import SingleFlight
from app.cache.tiered import TieredCache

__all__ = [
    "SYSTEM_CLOCK",
    "BytesCodec",
    "Cache",
    "CacheBackend",
    "CacheBackendError",
    "CacheConfig",
    "CacheEntry",
    "CacheError",
    "CacheManager",
    "CacheMetrics",
    "CachedFunction",
    "Clock",
    "Codec",
    "FakeClock",
    "JsonCodec",
    "MemoryCache",
    "NamespaceStats",
    "NullCache",
    "PickleCodec",
    "RedisCache",
    "SerializationError",
    "SingleFlight",
    "SingleFlightError",
    "SystemClock",
    "TieredCache",
    "binary_redis_from_url",
    "build_cache_manager",
    "build_cache_manager_from_settings",
    "cached",
    "derive_key",
    "fingerprint",
    "memory_cache",
    "null_cache",
    "qualify",
    "redis_cache",
    "redis_lock_factory",
    "tag_key_for",
    "tiered_cache",
]
