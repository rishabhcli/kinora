"""Response cache + in-flight dedup keyed by a stable request hash (§12.3).

Two of the §12.3 caching layers live here, both keyed by a content hash of the
*request* (model + op + canonicalized payload):

* **Response cache** — a bounded TTL+LRU of completed results. A re-read or an
  unchanged shot after a Director edit costs zero (§8.7 / §11.1: "Cache dedup").
* **Request-level dedup** — when two callers ask for the *same* key while the
  first is still in flight, the second awaits the first's result instead of
  issuing a duplicate call ("paying twice for a shot two sessions request
  simultaneously", §12.3). This is single-flight / coalescing.

Determinism: the hash is computed from a canonical JSON of the request parts, so
dict ordering and float formatting never change the key. The cache is generic over
the result type and never assumes a particular provider shape.

This module is pure data-structure + asyncio plumbing; it neither calls a provider
nor reads settings. The gateway wires it around real calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

R = TypeVar("R")

#: Injectable monotonic clock (seconds). Tests pass a controllable fake.
Clock = Callable[[], float]


def request_hash(model: str, op: str, payload: Any) -> str:
    """A stable hex digest of a request (model + op + canonical payload).

    The payload is canonicalized with sorted keys and compact separators so two
    semantically-identical requests (different dict order, same content) hash the
    same. Non-JSON-serializable parts fall back to ``repr`` so the function never
    raises — a slightly coarser key is better than a crash on the hot path.
    """
    try:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    except (TypeError, ValueError):
        body = repr(payload)
    digest = hashlib.sha256(f"{model}\x1f{op}\x1f{body}".encode()).hexdigest()
    return digest


def _json_default(obj: Any) -> Any:
    # bytes -> length+digest (don't hash megabytes of image data inline), else repr.
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes__": len(obj), "sha": hashlib.sha256(bytes(obj)).hexdigest()[:16]}
    return repr(obj)


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """Tunables for :class:`ResponseCache`."""

    max_entries: int = 512
    ttl_s: float = 300.0
    #: When True, identical concurrent requests coalesce to one in-flight call.
    single_flight: bool = True

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if self.ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")


@dataclass
class CacheStats:
    """Hit/miss/coalesce counters (telemetry + tests)."""

    hits: int = 0
    misses: int = 0
    coalesced: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.lookups
        return self.hits / total if total else 0.0


@dataclass(slots=True)
class _Entry(Generic[R]):
    value: R
    expires_at: float


class ResponseCache(Generic[R]):
    """A bounded TTL+LRU response cache with single-flight request coalescing.

    Generic over the cached result type ``R``. The cache key is any
    :class:`~collections.abc.Hashable` (use :func:`request_hash`). The
    :meth:`get_or_compute` method is the single entry point that callers use; it
    handles hit / miss / in-flight-coalesce / TTL-expiry atomically.
    """

    def __init__(self, config: CacheConfig | None = None, *, clock: Clock = time.monotonic) -> None:
        self.config = config or CacheConfig()
        self._clock = clock
        self._entries: OrderedDict[Hashable, _Entry[R]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future[R]] = {}
        self._lock = asyncio.Lock()
        self.stats = CacheStats()

    # -- low-level ops ---------------------------------------------------- #

    def _get_fresh_locked(self, key: Hashable) -> _Entry[R] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            self.stats.expirations += 1
            return None
        self._entries.move_to_end(key)
        return entry

    def _store_locked(self, key: Hashable, value: R) -> None:
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + self.config.ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > self.config.max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    async def get(self, key: Hashable) -> R | None:
        """Return a fresh cached value (counting a hit/miss), else ``None``."""
        async with self._lock:
            entry = self._get_fresh_locked(key)
            if entry is None:
                self.stats.misses += 1
                return None
            self.stats.hits += 1
            return entry.value

    async def set(self, key: Hashable, value: R) -> None:
        """Insert/replace a value (resets its TTL)."""
        async with self._lock:
            self._store_locked(key, value)

    async def invalidate(self, key: Hashable) -> bool:
        """Drop a key; returns True if it was present."""
        async with self._lock:
            return self._entries.pop(key, None) is not None

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # -- the single entry point ------------------------------------------ #

    async def get_or_compute(self, key: Hashable, compute: Callable[[], Awaitable[R]]) -> R:
        """Return a cached value, or compute + cache it, coalescing concurrent calls.

        On a fresh hit: return immediately. On a miss with ``single_flight``: the
        first caller runs ``compute`` while later callers awaiting the same key are
        parked on its future (counted as ``coalesced``) and get the same result —
        they never issue a duplicate provider call. A computation error is *not*
        cached; it propagates to every waiter so each can retry independently.
        """
        # Fast path: a fresh hit under the lock.
        async with self._lock:
            entry = self._get_fresh_locked(key)
            if entry is not None:
                self.stats.hits += 1
                return entry.value
            self.stats.misses += 1
            if self.config.single_flight:
                inflight = self._inflight.get(key)
                if inflight is not None:
                    self.stats.coalesced += 1
                    waiter = inflight
                else:
                    waiter = None
                    leader: asyncio.Future[R] = asyncio.get_running_loop().create_future()
                    self._inflight[key] = leader
            else:
                waiter = None
                leader = None  # type: ignore[assignment]

        if self.config.single_flight and waiter is not None:
            # Park on the leader's result outside the lock.
            return await waiter

        if not self.config.single_flight:
            value = await compute()
            await self.set(key, value)
            return value

        # We are the leader: run compute, publish to followers, store on success.
        try:
            value = await compute()
        except BaseException as exc:  # noqa: BLE001 - propagate to all waiters
            async with self._lock:
                self._inflight.pop(key, None)
            if not leader.done():
                leader.set_exception(exc)
                # If no follower ever awaits this future, asyncio would log
                # "exception was never retrieved" at GC; pre-retrieve it so the
                # warning is suppressed (the leader re-raises ``exc`` regardless).
                leader.add_done_callback(lambda fut: fut.exception())
            raise
        async with self._lock:
            self._store_locked(key, value)
            self._inflight.pop(key, None)
        if not leader.done():
            leader.set_result(value)
        return value


__all__ = [
    "CacheConfig",
    "CacheStats",
    "Clock",
    "ResponseCache",
    "request_hash",
]
