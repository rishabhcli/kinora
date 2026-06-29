"""Query-result cache with precise, dependency-based invalidation.

A cached entry is keyed by ``(query fingerprint, parameter hash)`` and carries the
exact set of base tables it was computed from. A write to table ``T`` invalidates
*only* the entries whose dependency set contains ``T`` — never the whole cache.
For hot single-row reads, callers may additionally scope an entry to specific row
keys (e.g. ``book_id = X``); a write that names those keys invalidates only the
matching entries, leaving same-table entries for other rows hot.

This is §8.7's shot-hash discipline ("memory means we never re-render a shot that
already passed QA") generalised from one render artifact to arbitrary query
results: the dependency set is the cache's equivalent of ``reference_set_hash``.

Properties:

* **Bounded** — an LRU with a configurable capacity; the least-recently-used
  entry is evicted on overflow.
* **Expiring** — each entry has an optional TTL; an expired entry is a miss (and
  is removed lazily on access and eagerly on a sweep).
* **Precise** — a reverse index ``table -> {keys}`` and ``(table,row) -> {keys}``
  drives O(affected) invalidation; no full scans.
* **Deterministic** — ``now`` is injectable; nothing here touches a clock you
  cannot control in a test.
* **In-process** — pure Python state, no Redis/network. A distributed backend can
  be layered behind the same :class:`ResultCache` interface later.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.datascale.optimize.errors import CacheError
from app.datascale.optimize.fingerprint import fingerprint, referenced_tables

V = TypeVar("V")


# --------------------------------------------------------------------------- #
# Key building
# --------------------------------------------------------------------------- #


def _hash_params(params: Any) -> str:
    """Stable hash of query parameters (order-independent for dict/mapping).

    Raises :class:`CacheError` for un-serialisable parameters so a caller learns
    immediately rather than silently caching under a colliding key.
    """
    try:
        if params is None:
            payload = "null"
        elif isinstance(params, dict):
            payload = json.dumps(params, sort_keys=True, default=_json_default)
        elif isinstance(params, (list, tuple)):
            payload = json.dumps(list(params), default=_json_default)
        else:
            payload = json.dumps(params, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise CacheError(f"cache parameters are not serialisable: {exc}") from exc
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - id, not crypto


def _json_default(obj: object) -> str:
    # Best-effort stable string for non-JSON scalars (UUID, datetime, Decimal…).
    return str(obj)


def make_cache_key(sql: str, params: Any = None) -> str:
    """Build the canonical cache key for a query + its parameters."""
    return f"{fingerprint(sql)}:{_hash_params(params)}"


@dataclass(frozen=True, slots=True)
class RowScope:
    """A (table, column, value) row-level dependency scope for fine invalidation."""

    table: str
    column: str
    value: object

    def token(self) -> str:
        """A hashable token identifying this row scope."""
        return f"{self.table}.{self.column}={self.value!r}"


# --------------------------------------------------------------------------- #
# Cache entry
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Entry(Generic[V]):
    value: V
    tables: frozenset[str]
    row_tokens: frozenset[str]
    expires_at: float | None
    created_at: float

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(slots=True)
class CacheStats:
    """Counters for the metrics panel."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0
    expirations: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "expirations": self.expirations,
            "hit_rate": round(self.hit_rate, 4),
        }


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

_MISS = object()


class ResultCache(Generic[V]):
    """An LRU+TTL query-result cache with dependency-precise invalidation."""

    def __init__(
        self,
        *,
        capacity: int = 1024,
        default_ttl_s: float | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if capacity <= 0:
            raise CacheError("capacity must be positive")
        self._capacity = capacity
        self._default_ttl_s = default_ttl_s
        self._now = now or time.monotonic
        self._entries: OrderedDict[str, _Entry[V]] = OrderedDict()
        # Reverse indexes for O(affected) invalidation.
        self._by_table: dict[str, set[str]] = {}
        self._by_row: dict[str, set[str]] = {}
        self.stats = CacheStats()

    # ---- core get/put ---- #

    def get(self, sql: str, params: Any = None) -> V | None:
        """Return a cached value or ``None`` (a miss / expiry)."""
        key = make_cache_key(sql, params)
        return self.get_by_key(key)

    def get_by_key(self, key: str) -> V | None:
        """Return a cached value by an explicit key."""
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.is_expired(self._now()):
            self._remove(key)
            self.stats.expirations += 1
            self.stats.misses += 1
            return None
        self._entries.move_to_end(key)  # mark recently used
        self.stats.hits += 1
        return entry.value

    def put(
        self,
        sql: str,
        value: V,
        *,
        params: Any = None,
        dependencies: Iterable[str] | None = None,
        row_scopes: Iterable[RowScope] | None = None,
        ttl_s: float | None = _MISS,  # type: ignore[assignment]
    ) -> str:
        """Cache ``value`` for ``sql``+``params`` with a dependency set.

        ``dependencies`` overrides the auto-derived table set (recommended for
        precision — the lexical deriver over-collects, which only over-invalidates
        but is safe). ``row_scopes`` adds row-level dependencies for fine
        invalidation. ``ttl_s`` defaults to the cache's ``default_ttl_s``; pass
        ``None`` explicitly for no expiry. Returns the cache key.
        """
        key = make_cache_key(sql, params)
        tables = (
            frozenset(t.lower() for t in dependencies)
            if dependencies is not None
            else referenced_tables(sql)
        )
        scopes = frozenset(s.token() for s in (row_scopes or ()))
        effective_ttl = self._default_ttl_s if ttl_s is _MISS else ttl_s
        expires_at = None if effective_ttl is None else self._now() + effective_ttl
        # Replace any prior entry (and its index links) under this key.
        if key in self._entries:
            self._remove(key)
        entry = _Entry(
            value=value,
            tables=tables,
            row_tokens=scopes,
            expires_at=expires_at,
            created_at=self._now(),
        )
        self._entries[key] = entry
        for table in tables:
            self._by_table.setdefault(table, set()).add(key)
        for tok in scopes:
            self._by_row.setdefault(tok, set()).add(key)
        self._evict_if_needed()
        return key

    def get_or_compute(
        self,
        sql: str,
        compute: Callable[[], V],
        *,
        params: Any = None,
        dependencies: Iterable[str] | None = None,
        row_scopes: Iterable[RowScope] | None = None,
        ttl_s: float | None = _MISS,  # type: ignore[assignment]
    ) -> V:
        """Return the cached value, computing + caching it on a miss."""
        hit = self.get(sql, params)
        if hit is not None:
            return hit
        value = compute()
        self.put(
            sql,
            value,
            params=params,
            dependencies=dependencies,
            row_scopes=row_scopes,
            ttl_s=ttl_s,
        )
        return value

    # ---- invalidation ---- #

    def invalidate_table(self, table: str) -> int:
        """Drop every entry depending on ``table``. Returns the count removed."""
        keys = self._by_table.get(table.lower(), set())
        return self._drop_keys(set(keys))

    def invalidate_tables(self, tables: Iterable[str]) -> int:
        """Drop entries depending on any of ``tables``."""
        keys: set[str] = set()
        for table in tables:
            keys |= self._by_table.get(table.lower(), set())
        return self._drop_keys(keys)

    def invalidate_row(self, scope: RowScope) -> int:
        """Drop entries scoped to a specific row (e.g. ``book_id = X``)."""
        keys = self._by_row.get(scope.token(), set())
        return self._drop_keys(set(keys))

    def invalidate_write(
        self, table: str, *, row_scopes: Iterable[RowScope] | None = None
    ) -> int:
        """Invalidate after a write to ``table``.

        When ``row_scopes`` is given, only entries scoped to those rows *or* entries
        that depend on the whole table without a row scope are dropped — keeping
        same-table entries for *other* rows hot. When omitted, the whole table's
        entries are invalidated (the safe coarse path).
        """
        if row_scopes is None:
            return self.invalidate_table(table)
        scopes = list(row_scopes)
        keys: set[str] = set()
        # Entries scoped to exactly these rows.
        for scope in scopes:
            keys |= self._by_row.get(scope.token(), set())
        # Plus table-dependent entries that carry NO row scope (cannot prove they
        # exclude the written row, so they must go).
        for k in self._by_table.get(table.lower(), set()):
            entry = self._entries.get(k)
            if entry is not None and not entry.row_tokens:
                keys.add(k)
        return self._drop_keys(keys)

    def clear(self) -> None:
        """Drop all entries (counters retained)."""
        self._entries.clear()
        self._by_table.clear()
        self._by_row.clear()

    def sweep_expired(self) -> int:
        """Eagerly remove all expired entries. Returns the count removed."""
        now = self._now()
        expired = [k for k, e in self._entries.items() if e.is_expired(now)]
        for k in expired:
            self._remove(k)
        self.stats.expirations += len(expired)
        return len(expired)

    # ---- introspection ---- #

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def keys_for_table(self, table: str) -> frozenset[str]:
        """The cache keys currently depending on ``table`` (for debugging/tests)."""
        return frozenset(self._by_table.get(table.lower(), set()))

    # ---- internals ---- #

    def _drop_keys(self, keys: set[str]) -> int:
        count = 0
        for key in keys:
            if key in self._entries:
                self._remove(key)
                count += 1
        self.stats.invalidations += count
        return count

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        for table in entry.tables:
            s = self._by_table.get(table)
            if s is not None:
                s.discard(key)
                if not s:
                    del self._by_table[table]
        for tok in entry.row_tokens:
            s = self._by_row.get(tok)
            if s is not None:
                s.discard(key)
                if not s:
                    del self._by_row[tok]

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._capacity:
            # LRU = first item in the OrderedDict.
            oldest_key = next(iter(self._entries))
            self._remove(oldest_key)
            self.stats.evictions += 1


__all__ = [
    "CacheStats",
    "ResultCache",
    "RowScope",
    "make_cache_key",
]
