"""Per-namespace cache metrics — hit / miss / evict / set / error counters.

This is a self-contained, dependency-free counter set (a plain thread-safe
``dict`` of ints plus a couple of derived ratios). It deliberately does **not**
register against the shared ``app.observability.metrics`` Prometheus registry —
that surface is owned by another package and editing it here would not be
additive. A caller that wants Prometheus export can read :meth:`snapshot` and
forward it; an in-process dashboard can poll :meth:`hit_rate` directly.

Counters are keyed by namespace so one ``CacheMetrics`` instance can back many
logical caches. The §12.5 observability spec calls for per-namespace
hit/miss/evict, which is exactly what :meth:`snapshot` returns.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NamespaceStats:
    """An immutable point-in-time view of one namespace's counters."""

    namespace: str
    hits: int = 0
    misses: int = 0
    #: Hits served because the loader/backend produced a negative (absent) marker.
    negative_hits: int = 0
    sets: int = 0
    deletes: int = 0
    evictions: int = 0
    expirations: int = 0
    #: L1 hits and L2 hits broken out (tiered caches populate both).
    l1_hits: int = 0
    l2_hits: int = 0
    #: Times the single-flight leader computed a value while followers waited.
    loads: int = 0
    load_errors: int = 0
    backend_errors: int = 0
    #: Times an early-expiry recompute was triggered (stampede smoothing).
    early_expirations: int = 0

    @property
    def lookups(self) -> int:
        """Total hit + miss probes."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Hits / lookups in ``[0, 1]`` (0 when there were no lookups)."""
        total = self.lookups
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float | int | str]:
        """Flat dict (for JSON export / a metrics endpoint)."""
        return {
            "namespace": self.namespace,
            "hits": self.hits,
            "misses": self.misses,
            "negative_hits": self.negative_hits,
            "sets": self.sets,
            "deletes": self.deletes,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "l1_hits": self.l1_hits,
            "l2_hits": self.l2_hits,
            "loads": self.loads,
            "load_errors": self.load_errors,
            "backend_errors": self.backend_errors,
            "early_expirations": self.early_expirations,
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 6),
        }


# Field names mirrored from NamespaceStats (excluding derived properties).
_COUNTERS: tuple[str, ...] = (
    "hits",
    "misses",
    "negative_hits",
    "sets",
    "deletes",
    "evictions",
    "expirations",
    "l1_hits",
    "l2_hits",
    "loads",
    "load_errors",
    "backend_errors",
    "early_expirations",
)


class CacheMetrics:
    """Thread-safe per-namespace counter bag.

    All ``inc_*`` methods take a namespace and bump by ``n`` (default 1). Reads
    return a frozen :class:`NamespaceStats`; the live counters are never exposed
    by reference.
    """

    __slots__ = ("_data", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, int]] = {}

    def _bump(self, namespace: str, field: str, n: int) -> None:
        if n == 0:
            return
        with self._lock:
            bucket = self._data.get(namespace)
            if bucket is None:
                bucket = dict.fromkeys(_COUNTERS, 0)
                self._data[namespace] = bucket
            bucket[field] += n

    # --- typed one-liner emit helpers (mirror the observability style) --- #

    def inc_hit(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "hits", n)

    def inc_miss(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "misses", n)

    def inc_negative_hit(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "negative_hits", n)

    def inc_set(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "sets", n)

    def inc_delete(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "deletes", n)

    def inc_eviction(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "evictions", n)

    def inc_expiration(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "expirations", n)

    def inc_l1_hit(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "l1_hits", n)

    def inc_l2_hit(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "l2_hits", n)

    def inc_load(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "loads", n)

    def inc_load_error(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "load_errors", n)

    def inc_backend_error(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "backend_errors", n)

    def inc_early_expiration(self, namespace: str, n: int = 1) -> None:
        self._bump(namespace, "early_expirations", n)

    # --- reads --- #

    def stats(self, namespace: str) -> NamespaceStats:
        """Frozen view of one namespace (all-zero if never touched)."""
        with self._lock:
            bucket = dict(self._data.get(namespace, {}))
        return NamespaceStats(namespace=namespace, **{k: bucket.get(k, 0) for k in _COUNTERS})

    def snapshot(self) -> dict[str, NamespaceStats]:
        """Frozen view of every namespace seen so far."""
        with self._lock:
            namespaces = list(self._data.keys())
        return {ns: self.stats(ns) for ns in namespaces}

    def hit_rate(self, namespace: str) -> float:
        """Convenience: hit rate for one namespace."""
        return self.stats(namespace).hit_rate

    def reset(self, namespace: str | None = None) -> None:
        """Clear one namespace's counters, or all of them when ``None``."""
        with self._lock:
            if namespace is None:
                self._data.clear()
            else:
                self._data.pop(namespace, None)


__all__ = ["CacheMetrics", "NamespaceStats"]
