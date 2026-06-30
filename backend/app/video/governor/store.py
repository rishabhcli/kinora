"""The counter store the governor accounts through (redis-interface, fake in tests).

Quota accounting must be *shared* across every API/worker process that submits
video renders — a per-process counter would let N workers each believe they are
under a provider's requests-per-minute limit while collectively blowing through it.
So the governor reads and mutates counters through a small async interface that a
real deployment backs with Redis (``INCRBYFLOAT`` + ``PEXPIRE``, ``ZADD``/``ZCARD``
for sliding windows) and that tests back with a deterministic in-memory fake.

The interface is intentionally tiny and Redis-shaped:

* :meth:`GovernorStore.incr_window` — atomically add to a fixed (tumbling) window
  bucket keyed by ``(key, window_start)`` and return the new total. This is the
  primitive behind requests-per-minute, daily video-seconds, and monthly spend.
* :meth:`GovernorStore.read_window` — read a window bucket without mutating it.
* :meth:`GovernorStore.adjust_gauge` / :meth:`GovernorStore.read_gauge` — a signed
  gauge for *concurrent* in-flight jobs (incremented on submit, decremented on
  terminal completion). Floored at zero so a double-release can't drive it negative.

Nothing here imports redis; the production adapter lives behind the same Protocol
and is wired by the composition root, never on import.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol, runtime_checkable


@runtime_checkable
class GovernorStore(Protocol):
    """The async counter surface the governor accounts through.

    A production adapter maps each method onto Redis; the in-memory
    :class:`InMemoryGovernorStore` mirrors the exact semantics for tests. All
    methods are async so the production path can do real I/O while tests resolve
    immediately.
    """

    async def incr_window(
        self, key: str, window_start: int, amount: float, *, ttl_s: int
    ) -> float:
        """Add ``amount`` to the bucket ``(key, window_start)``; return the new total.

        ``ttl_s`` bounds how long an expired bucket lingers (so old windows are
        reclaimed). The first writer to a bucket sets its TTL.
        """
        ...

    async def read_window(self, key: str, window_start: int) -> float:
        """Read the bucket ``(key, window_start)`` (0.0 if absent/expired)."""
        ...

    async def adjust_gauge(self, key: str, delta: int) -> int:
        """Add ``delta`` to the gauge ``key`` (floored at 0); return the new value."""
        ...

    async def read_gauge(self, key: str) -> int:
        """Read the gauge ``key`` (0 if absent)."""
        ...


class InMemoryGovernorStore:
    """A deterministic in-memory :class:`GovernorStore` for tests and local runs.

    Buckets are kept in a plain dict keyed by ``(key, window_start)``; the TTL is
    recorded but only enforced lazily by :meth:`expire_before` so a fake clock
    drives reclamation explicitly (no background sweeper, no wall-clock). A single
    lock serialises mutations so concurrent ``asyncio`` tasks see linearizable
    counters — the property a real Redis ``INCRBYFLOAT`` gives us.
    """

    def __init__(self) -> None:
        self._windows: dict[tuple[str, int], float] = {}
        self._window_expiry: dict[tuple[str, int], int] = {}
        self._gauges: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def incr_window(
        self, key: str, window_start: int, amount: float, *, ttl_s: int
    ) -> float:
        bucket = (key, window_start)
        async with self._lock:
            total = self._windows.get(bucket, 0.0) + amount
            self._windows[bucket] = total
            # First writer sets the expiry; later writers don't extend it (mirrors a
            # SET-once TTL on the bucket so a window can't be kept alive forever).
            self._window_expiry.setdefault(bucket, window_start + ttl_s)
            return total

    async def read_window(self, key: str, window_start: int) -> float:
        return self._windows.get((key, window_start), 0.0)

    async def adjust_gauge(self, key: str, delta: int) -> int:
        async with self._lock:
            value = max(0, self._gauges[key] + delta)
            self._gauges[key] = value
            return value

    async def read_gauge(self, key: str) -> int:
        return self._gauges.get(key, 0)

    # -- test/maintenance helpers (not part of the Protocol) -------------- #

    def expire_before(self, now_epoch: int) -> int:
        """Drop window buckets whose TTL has elapsed; return how many were removed.

        Lets a test (or a periodic sweep) reclaim stale buckets deterministically by
        passing the current epoch second from its fake clock.
        """
        stale = [b for b, exp in self._window_expiry.items() if exp <= now_epoch]
        for bucket in stale:
            self._windows.pop(bucket, None)
            self._window_expiry.pop(bucket, None)
        return len(stale)

    def snapshot(self) -> dict[str, object]:
        """A copy of the current state, for assertions/inspection."""
        return {
            "windows": dict(self._windows),
            "gauges": dict(self._gauges),
        }


__all__ = ["GovernorStore", "InMemoryGovernorStore"]
