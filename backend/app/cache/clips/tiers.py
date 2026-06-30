"""Assemble the clip cache's L1 -> L2 -> L3 tier stack.

Three storage tiers, fastest first:

* **L1** — in-process LRU/TTL (:class:`~app.cache.memory.MemoryCache`): nanosecond
  hits, emptied on restart, bounded by ``l1_max_entries``.
* **L2** — Redis (:class:`~app.cache.redis_backend.RedisCache`): shared across the
  api / render-worker / scheduler processes, survives a single process restart,
  expires on its TTL.
* **L3** — object store (:class:`~app.cache.clips.store.ObjectStoreCacheBackend`):
  durable, fleet-wide, the source of truth that lets a cold process discover an
  already-rendered clip and serve it for zero video-seconds.

The existing :class:`~app.cache.tiered.TieredCache` composes exactly two backends
(read-through + promotion + write-through), so a three-tier stack is just a
nested pair: ``Tiered(L1, Tiered(L2, L3))``. A read checks L1, then L2 (promoting
to L1), then L3 (promoting to L2 and thence L1) — so the *second* fleet-wide
request for a clip a cold process renders is an L3 hit that warms the faster
tiers. Any subset of tiers is valid: with no Redis it is ``Tiered(L1, L3)``; with
neither Redis nor object store it is L1-only.

Promotion respects each entry's remaining TTL; the durable L3 tier should be
configured with a long (or no) TTL so it outlives the faster tiers.
"""

from __future__ import annotations

import random

from app.cache.cache import Cache, CacheConfig
from app.cache.clips.store import ClipBlobStore, ObjectStoreCacheBackend
from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.codecs import Codec, JsonCodec
from app.cache.interface import CacheBackend
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics
from app.cache.redis_backend import RedisCache
from app.cache.tiered import TieredCache

#: Records are plain JSON dicts, so the portable JSON codec is the natural fit for
#: the Redis tier (debuggable in ``redis-cli`` and round-trips a ``ClipRecord``).
_RECORD_CODEC: Codec = JsonCodec(sort_keys=True)


def build_clip_backend(
    *,
    namespace: str,
    metrics: CacheMetrics,
    clock: Clock,
    l1_max_entries: int = 2048,
    redis: object | None = None,
    redis_prefix: str = "kinora:clipcache",
    object_store: ClipBlobStore | None = None,
    object_prefix: str = "clipcache/records",
    l2_fail_open: bool = True,
) -> CacheBackend:
    """Compose the available tiers into one :class:`CacheBackend`.

    ``redis`` (a binary-mode async client) enables L2; ``object_store`` enables
    the durable L3 tier. With neither, the result is a pure in-process L1 backend.
    """
    l1: CacheBackend = MemoryCache(
        max_entries=l1_max_entries, clock=clock, metrics=metrics, metrics_namespace=namespace
    )

    durable: CacheBackend | None = None
    if object_store is not None:
        durable = ObjectStoreCacheBackend(object_store, prefix=object_prefix, clock=clock)

    l2: CacheBackend | None = None
    if redis is not None:
        l2 = RedisCache(
            redis, prefix=f"{redis_prefix}:{namespace}", codec=_RECORD_CODEC, clock=clock
        )

    # Build the lower stack (everything below L1).
    lower: CacheBackend | None
    if l2 is not None and durable is not None:
        lower = TieredCache(
            l2,
            durable,
            clock=clock,
            metrics=metrics,
            metrics_namespace=namespace,
            l2_fail_open=l2_fail_open,
        )
    elif l2 is not None:
        lower = l2
    elif durable is not None:
        lower = durable
    else:
        lower = None

    if lower is None:
        return l1
    return TieredCache(
        l1,
        lower,
        clock=clock,
        metrics=metrics,
        metrics_namespace=namespace,
        l2_fail_open=l2_fail_open,
    )


def build_clip_cache(
    *,
    namespace: str = "clipcache",
    config: CacheConfig | None = None,
    clock: Clock | None = None,
    metrics: CacheMetrics | None = None,
    rng: random.Random | None = None,
    l1_max_entries: int = 2048,
    redis: object | None = None,
    redis_prefix: str = "kinora:clipcache",
    object_store: ClipBlobStore | None = None,
    object_prefix: str = "clipcache/records",
    l2_fail_open: bool = True,
) -> Cache[dict]:
    """Build the low-level :class:`~app.cache.cache.Cache` over the tier stack.

    The returned facade stores raw ``dict`` records; the
    :class:`~app.cache.clips.dedup.RenderCache` wraps it with the typed
    :class:`~app.cache.clips.record.ClipRecord` API.
    """
    clk = clock or SYSTEM_CLOCK
    mx = metrics or CacheMetrics()
    backend = build_clip_backend(
        namespace=namespace,
        metrics=mx,
        clock=clk,
        l1_max_entries=l1_max_entries,
        redis=redis,
        redis_prefix=redis_prefix,
        object_store=object_store,
        object_prefix=object_prefix,
        l2_fail_open=l2_fail_open,
    )
    return Cache(backend, namespace=namespace, config=config, clock=clk, metrics=mx, rng=rng)


__all__ = ["build_clip_backend", "build_clip_cache"]
