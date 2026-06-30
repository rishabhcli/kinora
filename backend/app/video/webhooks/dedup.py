"""Idempotent delivery dedup for at-least-once provider callbacks.

Async media providers deliver callbacks **at least once**: the same completion
event can arrive two or three times (network retries, provider redelivery after a
slow ACK). Processing it twice would double-persist an asset and — far worse —
double-charge the §11 video-seconds budget. So before handing a callback to the
:class:`~app.video.webhooks.models.JobCompletionSink` the gateway claims its
``dedup_key`` exactly once; a second arrival within the retention window is a
no-op ``replayed`` outcome.

This mirrors the render queue's ``shot_hash`` idempotency (kinora.md §12.1) at
the *ingress* layer. The default store is an in-memory TTL set with a hard size
cap — perfect for tests and a single API process — behind a :class:`DedupStore`
Protocol so the orchestrator can drop in a Redis ``SET key val NX PX`` claim for
the multi-process production deployment without touching the gateway.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class DedupStore(Protocol):
    """A first-writer-wins claim store keyed by a callback's ``dedup_key``."""

    async def claim(self, key: str) -> bool:
        """Atomically claim ``key``; return ``True`` for the first caller only.

        A return of ``False`` means the key was already claimed (a duplicate
        delivery) and the callback must not be processed again.
        """
        ...


class InMemoryDedupStore:
    """A process-local TTL claim store with a bounded LRU eviction.

    Thread-safety note: the FastAPI app runs callbacks on a single event loop, so
    the await-free critical section here is effectively atomic for the in-process
    case. The Protocol exists precisely so a cross-process deployment swaps this
    for a Redis-atomic claim.
    """

    def __init__(
        self,
        *,
        ttl_s: float = 86_400.0,
        max_entries: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        #: key -> expiry timestamp; ordered for cheap LRU/expiry eviction.
        self._seen: OrderedDict[str, float] = OrderedDict()

    async def claim(self, key: str) -> bool:
        now = self._clock()
        self._evict_expired(now)
        existing = self._seen.get(key)
        if existing is not None and existing > now:
            # Touch for LRU recency, but report as a duplicate.
            self._seen.move_to_end(key)
            return False
        self._seen[key] = now + self._ttl_s
        self._seen.move_to_end(key)
        self._enforce_capacity()
        return True

    def seen_count(self) -> int:
        """The number of live (un-expired) claims — for tests/metrics."""
        self._evict_expired(self._clock())
        return len(self._seen)

    def _evict_expired(self, now: float) -> None:
        # Entries are appended with monotonically increasing expiries only when
        # ttl is constant; to stay correct under a custom clock we scan from the
        # front (oldest) and stop at the first live entry.
        stale: list[str] = []
        for key, expiry in self._seen.items():
            if expiry > now:
                break
            stale.append(key)
        for key in stale:
            del self._seen[key]

    def _enforce_capacity(self) -> None:
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)  # drop the least-recently-used


__all__ = ["DedupStore", "InMemoryDedupStore"]
