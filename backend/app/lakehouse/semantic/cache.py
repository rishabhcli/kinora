"""Query-result caching keyed by the compiled-plan fingerprint + access scope.

Recomputing a dashboard tile that hasn't changed is waste; the result cache
memoises a :class:`~app.lakehouse.semantic.executor.MetricResult` against a
**stable cache key**. The key folds in:

* the :pyattr:`QueryPlan.fingerprint` (the full compiled plan — metrics,
  dimensions, filters, grain, ordering, limit), so two textually-different
  queries that compile to the same plan share a hit; and
* an **access scope token** (a digest of the governance row-filter + masked
  columns), so two principals with different row-level visibility *never* share a
  cache entry — a correctness requirement, not an optimisation.

The cache is an in-process TTL + LRU map (thread-safe, monotonic-clock based) so
it works with no Redis in tests and degrades to a bounded memory footprint in
production. The host can swap in a distributed backend behind the
:class:`ResultCache` Protocol.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from app.lakehouse.semantic.executor import MetricResult
from app.lakehouse.semantic.plan import QueryPlan


def scope_token(row_filter_repr: str | None, masked: frozenset[str]) -> str:
    """A short, stable token for a principal's access scope (row filter + masks)."""
    payload = f"{row_filter_repr or ''}|{','.join(sorted(masked))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cache_key(plan: QueryPlan, scope: str = "") -> str:
    """Compose the full cache key from a plan fingerprint + access scope."""
    return f"{plan.fingerprint()}:{scope}" if scope else plan.fingerprint()


class ResultCache(Protocol):
    """A get/put cache over :class:`MetricResult` keyed by string."""

    def get(self, key: str) -> MetricResult | None:
        ...

    def put(self, key: str, value: MetricResult) -> None:
        ...


@dataclass
class CacheStats:
    """Observable counters for the cache (read by the service + tests)."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


@dataclass
class _Entry:
    value: MetricResult
    expires_at: float


class InMemoryResultCache:
    """A thread-safe TTL + LRU result cache (monotonic clock, bounded size)."""

    def __init__(
        self,
        *,
        max_entries: int = 512,
        ttl_seconds: float = 300.0,
        clock: Clock | None = None,
    ):
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock or _MonotonicClock()
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self.stats = CacheStats()

    def get(self, key: str) -> MetricResult | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            if entry.expires_at <= self._clock.now():
                del self._entries[key]
                self.stats.expirations += 1
                self.stats.misses += 1
                return None
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return entry.value

    def put(self, key: str, value: MetricResult) -> None:
        with self._lock:
            self._entries[key] = _Entry(
                value=value, expires_at=self._clock.now() + self._ttl
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
                self.stats.evictions += 1

    def invalidate(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class Clock(Protocol):
    def now(self) -> float:
        ...


class _MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


__all__ = [
    "CacheStats",
    "Clock",
    "InMemoryResultCache",
    "ResultCache",
    "cache_key",
    "scope_token",
]
