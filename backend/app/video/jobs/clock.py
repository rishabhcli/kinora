"""Clocks for the engine: a real monotonic clock and a deterministic fake.

The engine reads time and sleeps *only* through a :class:`~app.video.jobs.ports.JobClock`,
so production gets real backoff waits and tests get instant, reproducible time.
:class:`ManualClock` advances ``now`` exactly by each requested ``sleep`` without
ever blocking, which is what makes "poll N times then succeed", deadline-expiry,
and webhook/poll-race tests deterministic.
"""

from __future__ import annotations

import asyncio
import time


class SystemClock:
    """A real clock: monotonic ``now`` + ``asyncio.sleep``."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualClock:
    """A deterministic fake clock that advances on ``sleep`` without waiting.

    ``await clock.sleep(s)`` yields control once (so concurrent tasks interleave
    realistically) and advances ``now`` by ``s`` — no wall-clock delay. Tests can
    also call :meth:`advance` to move time forward explicitly (e.g. to cross a
    deadline) and :meth:`set` to pin an absolute value.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        # Yield so other awaiting tasks (a racing webhook, the await loop) run,
        # then advance virtual time. Never touches the wall clock.
        await asyncio.sleep(0)
        if seconds > 0:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds`` (no yield)."""
        self._now += seconds

    def set(self, value: float) -> None:
        """Pin virtual time to an absolute value."""
        self._now = value


__all__ = ["ManualClock", "SystemClock"]
