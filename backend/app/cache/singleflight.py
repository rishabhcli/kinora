"""Single-flight — collapse concurrent loads of the same key into one call.

When N coroutines all miss the same key at once and each would call an expensive
loader (a DashScope round-trip, a DB query), single-flight lets exactly **one**
of them run the loader while the rest ``await`` the same in-flight task and share
its result. This is the in-process half of the §12.3 "request-level dedup" row
(`in-flight shot_hash → paying twice for a shot two sessions request
simultaneously`); the cross-process half is the Redis :class:`DistributedLock`,
which the facade layers on top.

Semantics:

* The leader's result is delivered to every follower; the leader's exception is
  re-raised in every follower (wrapped as :class:`SingleFlightError` for the
  followers, raised as-is for the leader).
* The in-flight entry is removed as soon as the leader finishes, so the *next*
  wave of callers triggers a fresh load rather than getting a stale cached
  failure.
* Cancellation of a follower never cancels the shared leader task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from app.cache.errors import SingleFlightError

T = TypeVar("T")


class SingleFlight(Generic[T]):
    """Coalesce concurrent ``do(key, loader)`` calls per key into one execution."""

    __slots__ = ("_inflight", "_lock")

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        """Run ``loader`` once per ``key`` for all concurrent callers.

        The first caller becomes the leader and runs ``loader`` inside a shared
        task; concurrent callers await that task. Returns the loader's result.
        """
        async with self._lock:
            task = self._inflight.get(key)
            is_leader = task is None
            if task is None:
                task = asyncio.ensure_future(loader())
                self._inflight[key] = task

        if is_leader:
            try:
                return await task
            finally:
                # Drop the in-flight entry so the next wave reloads.
                async with self._lock:
                    if self._inflight.get(key) is task:
                        del self._inflight[key]
        else:
            # Follower: share the leader's outcome, but never cancel the leader
            # if *this* follower is cancelled (shield the await).
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except SingleFlightError:
                raise
            except Exception as exc:  # noqa: BLE001 - share the leader's failure
                raise SingleFlightError(str(exc)) from exc

    def in_flight(self) -> int:
        """Number of keys currently being loaded (diagnostics)."""
        return len(self._inflight)


__all__ = ["SingleFlight"]
