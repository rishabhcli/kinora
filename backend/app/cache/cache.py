"""The :class:`Cache` facade — the one object call sites actually use.

It wraps any :class:`~app.cache.interface.CacheBackend` (L1, tiered, null, …)
and adds the rich behaviour that the §12.3 caching table implies:

* **Namespacing.** Every cache is scoped to a ``namespace``; keys are qualified
  so ``invalidate_namespace`` / metrics are coherent per logical cache.
* **Cache-aside** (:meth:`get` / :meth:`set` / :meth:`delete`).
* **Read-through** (:meth:`get_or_load`): on a miss, run a loader, store the
  result, return it — guarded by stampede protection.
* **Write-through** (:meth:`set` always writes through the backend; with a tiered
  backend that means both tiers).
* **Negative caching.** A loader that returns the configured *miss sentinel*
  (default ``None``) is cached as a short-TTL negative entry so repeated lookups
  of an absent key don't keep hammering the loader (the §8.5 "timely removal"
  also applies — negatives expire fast).
* **Stampede protection.** In-process :class:`~app.cache.singleflight.SingleFlight`
  collapses concurrent loads; an optional cross-process Redis lock collapses them
  across workers; probabilistic **early expiry** spreads refreshes out so the
  whole population never expires the same key at the same instant.
* **Tag + key invalidation.** Entries can carry tags; :meth:`invalidate_tag`
  drops a whole tag in one call (the cheap-Director-edit story of §8.7: change
  one character → invalidate that character's tag → only those shots re-render).
* **Metrics.** Every probe/store/evict bumps a per-namespace
  :class:`~app.cache.metrics.CacheMetrics`.

In-memory-only mode is just ``Cache(MemoryCache(), namespace=...)`` — no infra.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.interface import CacheBackend
from app.cache.keys import qualify
from app.cache.metrics import CacheMetrics
from app.cache.singleflight import SingleFlight

T = TypeVar("T")

#: Sentinel meaning "argument not supplied" for optional ``ttl`` parameters, so
#: ``None`` (a real, meaningful TTL value) is distinguishable from "use the
#: namespace default". Shared with the decorator so both agree.
ABSENT: Any = object()
#: Backwards-compatible private alias used internally.
_ABSENT = ABSENT


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """Per-cache policy knobs (immutable; one per logical namespace)."""

    namespace: str = "default"
    #: Default TTL applied to positive entries when a call omits one (None = no TTL).
    default_ttl: float | None = 300.0
    #: TTL for negative (absent) entries; short by §8.5 ("timely removal").
    negative_ttl: float | None = 30.0
    #: Whether a loader returning the miss sentinel is cached as a negative entry.
    cache_negatives: bool = True
    #: The value a loader returns to mean "absent" (negative-cached).
    miss_sentinel: Any = None
    #: XFetch aggressiveness; >1 refreshes earlier, 0 disables early expiry.
    early_expiry_beta: float = 1.0
    #: When True, backend transport errors degrade to a soft miss (fail-open).
    fail_open: bool = True


class Cache(Generic[T]):
    """The high-level, namespaced cache facade over a backend."""

    def __init__(
        self,
        backend: CacheBackend,
        *,
        namespace: str = "default",
        config: CacheConfig | None = None,
        clock: Clock | None = None,
        metrics: CacheMetrics | None = None,
        rng: random.Random | None = None,
        lock_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._backend = backend
        self._clock = clock or SYSTEM_CLOCK
        self._metrics = metrics or CacheMetrics()
        self._rng = rng or random.Random()
        # An optional callable name -> async context manager (a Redis lock) for
        # cross-process stampede protection. Defaults to in-process only.
        self._lock_factory = lock_factory
        self._sf: SingleFlight[Any] = SingleFlight()
        base = config or CacheConfig(namespace=namespace)
        # Honour an explicit namespace kwarg even if a config was passed.
        self._cfg = base if base.namespace == namespace else _with_ns(base, namespace)
        self._ns = self._cfg.namespace

    @property
    def namespace(self) -> str:
        return self._ns

    @property
    def metrics(self) -> CacheMetrics:
        return self._metrics

    @property
    def backend(self) -> CacheBackend:
        return self._backend

    def stats(self) -> Any:
        """This namespace's metrics snapshot."""
        return self._metrics.stats(self._ns)

    # --- key qualification --- #

    def _qkey(self, key: str) -> str:
        return qualify(self._ns, key)

    def _qtag(self, tag: str) -> str:
        # Tags live in their own sub-namespace so they can't collide with keys.
        return tag_key_for(self._ns, tag)

    def _qualify_tags(self, tags: Iterable[str] | None) -> frozenset[str]:
        """Map user tag names to the backend's qualified tag identifiers."""
        if tags is None:
            return frozenset()
        return frozenset(self._qtag(t) for t in tags)

    # --- cache-aside primitives --- #

    async def _read(self, key: str) -> CacheEntry | None:
        try:
            return await self._backend.get(self._qkey(key))
        except CacheBackendError:
            self._metrics.inc_backend_error(self._ns)
            if self._cfg.fail_open:
                return None
            raise

    async def get(self, key: str, default: Any = None) -> Any:
        """Cache-aside read. Returns the value, or ``default`` on a miss.

        A negative (absent) entry reads as a hit returning the miss sentinel.
        """
        entry = await self._read(key)
        if entry is None or entry.is_expired(self._clock.time()):
            self._metrics.inc_miss(self._ns)
            return default
        self._metrics.inc_hit(self._ns)
        if entry.negative:
            self._metrics.inc_negative_hit(self._ns)
            return self._cfg.miss_sentinel
        return entry.value

    async def get_entry(self, key: str) -> CacheEntry | None:
        """Lower-level read returning the live :class:`CacheEntry` (or ``None``)."""
        entry = await self._read(key)
        if entry is None or entry.is_expired(self._clock.time()):
            self._metrics.inc_miss(self._ns)
            return None
        self._metrics.inc_hit(self._ns)
        if entry.negative:
            self._metrics.inc_negative_hit(self._ns)
        return entry

    async def has(self, key: str) -> bool:
        """Whether a live (positive or negative) entry exists for ``key``."""
        entry = await self._read(key)
        return entry is not None and not entry.is_expired(self._clock.time())

    async def set(
        self,
        key: str,
        value: T,
        *,
        ttl: float | None | object = _ABSENT,
        tags: Iterable[str] | None = None,
    ) -> None:
        """Write-through store of ``value`` (uses ``default_ttl`` when omitted)."""
        effective_ttl = self._cfg.default_ttl if ttl is _ABSENT else cast("float | None", ttl)
        entry = CacheEntry.of(
            value,
            now=self._clock.time(),
            ttl=effective_ttl,
            tags=self._qualify_tags(tags),
        )
        await self._write(key, entry)

    async def set_negative(self, key: str, *, ttl: float | None | object = _ABSENT) -> None:
        """Store a negative (absent) marker so repeated misses are cheap."""
        effective_ttl = self._cfg.negative_ttl if ttl is _ABSENT else cast("float | None", ttl)
        entry = CacheEntry.of(None, now=self._clock.time(), ttl=effective_ttl, negative=True)
        await self._write(key, entry)

    async def _write(self, key: str, entry: CacheEntry) -> None:
        try:
            await self._backend.set(self._qkey(key), entry)
        except CacheBackendError:
            self._metrics.inc_backend_error(self._ns)
            if not self._cfg.fail_open:
                raise
            return
        self._metrics.inc_set(self._ns)

    async def delete(self, key: str) -> bool:
        try:
            removed = await self._backend.delete(self._qkey(key))
        except CacheBackendError:
            self._metrics.inc_backend_error(self._ns)
            if self._cfg.fail_open:
                return False
            raise
        if removed:
            self._metrics.inc_delete(self._ns)
        return removed

    async def delete_many(self, keys: Iterable[str]) -> int:
        scoped = [self._qkey(k) for k in keys]
        try:
            removed = await self._backend.delete_many(scoped)
        except CacheBackendError:
            self._metrics.inc_backend_error(self._ns)
            if self._cfg.fail_open:
                return 0
            raise
        if removed:
            self._metrics.inc_delete(self._ns, removed)
        return removed

    # --- read-through with stampede protection --- #

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl: float | None | object = _ABSENT,
        tags: Iterable[str] | None = None,
        cache_negatives: bool | None = None,
    ) -> T:
        """Read-through: serve a cached value or run ``loader``, store, and return.

        Concurrent callers for the same key are collapsed by single-flight (and,
        if a lock factory is configured, a cross-process lock). A live entry may
        be *probabilistically* treated as stale before its hard expiry so the
        population refreshes the key gradually (stampede smoothing).
        """
        now = self._clock.time()
        entry = await self._read(key)
        if entry is not None and not entry.is_expired(now):
            # Decide whether to volunteer for an early refresh.
            if self._cfg.early_expiry_beta > 0 and entry.should_early_expire(
                now, beta=self._cfg.early_expiry_beta, rng=self._rng
            ):
                self._metrics.inc_early_expiration(self._ns)
                # Fall through to recompute (still a hit for accounting purposes
                # is wrong — treat as a refresh; do NOT count a hit here).
            else:
                self._metrics.inc_hit(self._ns)
                if entry.negative:
                    self._metrics.inc_negative_hit(self._ns)
                    return cast("T", self._cfg.miss_sentinel)
                return cast("T", entry.value)
        else:
            self._metrics.inc_miss(self._ns)

        return await self._load_and_store(
            key, loader, ttl=ttl, tags=tags, cache_negatives=cache_negatives
        )

    async def _load_and_store(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl: float | None | object,
        tags: Iterable[str] | None,
        cache_negatives: bool | None,
    ) -> T:
        async def _runner() -> T:
            # Cross-process single-flight: if a lock is available, hold it while
            # we double-check + load so two workers don't both compute.
            if self._lock_factory is not None:
                return await self._locked_load(
                    key, loader, ttl=ttl, tags=tags, cache_negatives=cache_negatives
                )
            return await self._do_load(
                key, loader, ttl=ttl, tags=tags, cache_negatives=cache_negatives
            )

        # In-process single-flight keyed by the qualified key.
        return cast("T", await self._sf.do(self._qkey(key), _runner))

    async def _locked_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl: float | None | object,
        tags: Iterable[str] | None,
        cache_negatives: bool | None,
    ) -> T:
        assert self._lock_factory is not None
        lock = self._lock_factory(f"{self._ns}:lock:{key}")
        try:
            async with lock:
                # Re-check after acquiring: another worker may have populated it.
                entry = await self._read(key)
                if entry is not None and not entry.is_expired(self._clock.time()):
                    self._metrics.inc_hit(self._ns)
                    if entry.negative:
                        self._metrics.inc_negative_hit(self._ns)
                        return cast("T", self._cfg.miss_sentinel)
                    return cast("T", entry.value)
                return await self._do_load(
                    key, loader, ttl=ttl, tags=tags, cache_negatives=cache_negatives
                )
        except Exception:  # noqa: BLE001 - lock unavailable: fall back to a plain load
            return await self._do_load(
                key, loader, ttl=ttl, tags=tags, cache_negatives=cache_negatives
            )

    async def _do_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl: float | None | object,
        tags: Iterable[str] | None,
        cache_negatives: bool | None,
    ) -> T:
        self._metrics.inc_load(self._ns)
        try:
            value = await loader()
        except Exception:
            self._metrics.inc_load_error(self._ns)
            raise

        do_negatives = self._cfg.cache_negatives if cache_negatives is None else cache_negatives
        if value == self._cfg.miss_sentinel:
            # A loader "miss": cache the absence only when negative caching is on;
            # otherwise store nothing so every lookup re-runs the loader.
            if do_negatives:
                await self.set_negative(key)
            return value
        await self.set(key, value, ttl=ttl, tags=tags)
        return value

    # --- invalidation --- #

    async def invalidate(self, *keys: str) -> int:
        """Drop one or more keys (key-based invalidation)."""
        if not keys:
            return 0
        if len(keys) == 1:
            return 1 if await self.delete(keys[0]) else 0
        return await self.delete_many(keys)

    async def invalidate_tag(self, tag: str) -> int:
        """Drop every entry tagged ``tag`` (the §8.7 cheap-edit primitive)."""
        try:
            removed = await self._backend.delete_tag(self._qtag(tag))
        except CacheBackendError:
            self._metrics.inc_backend_error(self._ns)
            if self._cfg.fail_open:
                return 0
            raise
        if removed:
            self._metrics.inc_delete(self._ns, removed)
        return removed

    async def invalidate_namespace(self) -> None:
        """Drop everything in this cache's namespace.

        For a backend that owns a dedicated keyspace (Redis with a per-namespace
        prefix) this is a scoped clear; for a shared in-process backend it clears
        the whole backend, so prefer one backend per namespace if you rely on it.
        """
        with suppress(CacheBackendError):
            await self._backend.clear()

    # --- helpers --- #

    async def health(self) -> bool:
        return await self._backend.health()

    async def close(self) -> None:
        await self._backend.close()


def _with_ns(cfg: CacheConfig, namespace: str) -> CacheConfig:
    return CacheConfig(
        namespace=namespace,
        default_ttl=cfg.default_ttl,
        negative_ttl=cfg.negative_ttl,
        cache_negatives=cfg.cache_negatives,
        miss_sentinel=cfg.miss_sentinel,
        early_expiry_beta=cfg.early_expiry_beta,
        fail_open=cfg.fail_open,
    )


# Re-export so callers can `from app.cache.cache import _qtag_for` style tags.
def tag_key_for(namespace: str, tag: str) -> str:
    """The qualified tag identifier a backend stores for ``(namespace, tag)``.

    Exposed so a :class:`Cache` and an out-of-band invalidator agree on the tag
    keyspace without sharing a private method.
    """
    return f"{namespace}#tag#{tag}"


__all__ = [
    "ABSENT",
    "Cache",
    "CacheConfig",
    "tag_key_for",
]
