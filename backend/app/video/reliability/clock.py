"""Injectable monotonic time for the reliability coordinator.

Every deadline / elapsed-time decision in :mod:`app.video.reliability` reads
``Clock`` (seconds, monotonic) and waits via ``Sleep`` so the whole coordinator
is deterministic under test: a :class:`ManualClock` lets a test drive the
per-shot deadline, provider latencies, and the hedge timer to the microsecond
with **zero** real waiting — never a wall-clock ``asyncio.sleep`` in the hot
path. Mirrors the proven seam in ``app.providers.resilience`` so the two stacks
compose under one fake clock when the orchestrator wires them together.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

#: A monotonic clock returning fractional seconds. Production passes
#: :func:`time.monotonic`; tests pass a :class:`ManualClock`.
Clock = Callable[[], float]

#: An async sleep ``(seconds) -> None``. Production passes :func:`asyncio.sleep`;
#: tests pass :func:`make_manual_sleep` to advance virtual time instead of waiting.
Sleep = Callable[[float], Awaitable[None]]


class ManualClock:
    """A controllable monotonic clock for deterministic time-based tests.

    Not thread-safe by design — the coordinator is single-event-loop, and a
    cooperative ``manual_sleep`` advances this clock between ``await`` points.
    """

    __slots__ = ("now",)

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move virtual time forward (never backward)."""
        if seconds < 0:
            raise ValueError("cannot advance the clock backwards")
        self.now += seconds


def make_manual_sleep(clock: ManualClock) -> Sleep:
    """An async sleep that advances ``clock`` instead of waiting on the wall clock.

    It still yields to the event loop once (``asyncio.sleep(0)``) so concurrent
    tasks — e.g. a hedged second attempt — make progress; the time itself is
    virtual but cooperative scheduling is preserved.
    """

    async def _sleep(seconds: float) -> None:
        clock.advance(max(seconds, 0.0))
        await asyncio.sleep(0)

    return _sleep


__all__ = ["Clock", "ManualClock", "Sleep", "make_manual_sleep"]
