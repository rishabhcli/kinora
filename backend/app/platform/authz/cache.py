"""The decision cache — memoise ``check`` results with TTL + invalidation.

Authorization is read-heavy: the same ``(subject, action, resource)`` is asked
repeatedly within a request and across requests. The cache stores a decision
keyed by :attr:`AuthorizationRequest.cache_key` (which deliberately excludes the
wall-clock ``now``) for a short TTL, and exposes targeted **invalidation** so a
relationship/role change drops exactly the affected entries rather than flushing
everything.

Invalidation is by *tag*: every cached entry is tagged with the subject ref and
the resource ref it concerns, so ``invalidate_subject("user:alice")`` or
``invalidate_resource("book:42")`` evicts precisely the entries that could have
changed. A tuple write or a role grant calls the matching invalidation.

The default :class:`InMemoryDecisionCache` is a process-local TTL map (fine for a
single API process and for tests). The :class:`DecisionCache` protocol lets a
Redis-backed cache drop in for multi-process deployments without touching the
SDK. The cache is intentionally *correct-by-TTL*: a bounded staleness window is
accepted (the standard authz-cache tradeoff), and security-sensitive writes call
invalidation to shorten it to zero for the affected principal/resource.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from app.platform.authz.model import AuthorizationRequest, Decision


class Clock(Protocol):
    """A monotonic-ish time source (seconds); injectable for deterministic tests."""

    def __call__(self) -> float: ...


class DecisionCache(Protocol):
    """The cache seam used by the SDK (in-memory or Redis-backed)."""

    def get(self, request: AuthorizationRequest) -> Decision | None:
        """Return a fresh cached decision for ``request`` (or ``None``)."""
        ...

    def put(self, decision: Decision) -> None:
        """Cache ``decision`` under its request's key."""
        ...

    def invalidate_subject(self, subject_ref: str) -> int:
        """Evict every entry concerning ``subject_ref``; return the count."""
        ...

    def invalidate_resource(self, resource_ref: str) -> int:
        """Evict every entry concerning ``resource_ref``; return the count."""
        ...

    def clear(self) -> None:
        """Drop every entry."""
        ...


@dataclass
class _Entry:
    decision: Decision
    expires_at: float
    subject_ref: str
    resource_ref: str


class InMemoryDecisionCache:
    """A process-local TTL + LRU decision cache with tag invalidation.

    Bounded by ``max_entries`` (LRU eviction). ``ttl_s`` is the freshness window.
    Tag indexes (subject_ref / resource_ref → keys) make targeted invalidation
    O(affected) instead of a full scan.
    """

    def __init__(
        self,
        *,
        ttl_s: float = 30.0,
        max_entries: int = 10_000,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._by_subject: dict[str, set[str]] = {}
        self._by_resource: dict[str, set[str]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, request: AuthorizationRequest) -> Decision | None:
        key = request.cache_key
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= self._clock():
            self._evict(key)
            self.misses += 1
            return None
        self._entries.move_to_end(key)  # LRU touch
        self.hits += 1
        return entry.decision.with_flag(cached=True)

    def put(self, decision: Decision) -> None:
        request = decision.request
        key = request.cache_key
        entry = _Entry(
            decision=decision,
            expires_at=self._clock() + self._ttl,
            subject_ref=request.subject.ref,
            resource_ref=request.resource.ref,
        )
        if key in self._entries:
            self._evict(key)
        self._entries[key] = entry
        self._entries.move_to_end(key)
        self._by_subject.setdefault(entry.subject_ref, set()).add(key)
        self._by_resource.setdefault(entry.resource_ref, set()).add(key)
        while len(self._entries) > self._max:
            oldest, _ = self._entries.popitem(last=False)
            self._unindex(oldest)

    def invalidate_subject(self, subject_ref: str) -> int:
        return self._invalidate(self._by_subject.get(subject_ref, set()))

    def invalidate_resource(self, resource_ref: str) -> int:
        return self._invalidate(self._by_resource.get(resource_ref, set()))

    def clear(self) -> None:
        self._entries.clear()
        self._by_subject.clear()
        self._by_resource.clear()

    # -- internals ----------------------------------------------------------- #

    def _invalidate(self, keys: set[str]) -> int:
        count = 0
        for key in list(keys):
            if key in self._entries:
                self._evict(key)
                count += 1
        return count

    def _evict(self, key: str) -> None:
        self._entries.pop(key, None)
        self._unindex(key)

    def _unindex(self, key: str) -> None:
        for index in (self._by_subject, self._by_resource):
            for tag, keyset in list(index.items()):
                keyset.discard(key)
                if not keyset:
                    del index[tag]

    @property
    def size(self) -> int:
        return len(self._entries)


class NullDecisionCache:
    """A cache that never caches (for paths that must always re-evaluate)."""

    def get(self, request: AuthorizationRequest) -> Decision | None:
        return None

    def put(self, decision: Decision) -> None:
        return None

    def invalidate_subject(self, subject_ref: str) -> int:
        return 0

    def invalidate_resource(self, resource_ref: str) -> int:
        return 0

    def clear(self) -> None:
        return None


__all__ = [
    "Clock",
    "DecisionCache",
    "InMemoryDecisionCache",
    "NullDecisionCache",
]
