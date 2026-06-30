"""Clock seams — a real monotonic clock and a deterministic fake for tests."""

from __future__ import annotations

import time


class MonotonicClock:
    """The production :class:`~app.video.shadow.seams.Clock` (``time.monotonic``)."""

    def monotonic(self) -> float:  # noqa: D102 - trivial
        return time.monotonic()


class ManualClock:
    """A deterministic clock driven by hand — for latency-measurement tests.

    Starts at ``0.0`` and only advances when :meth:`advance` is called, so a test
    can make a render take *exactly* the latency it asserts on.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:  # noqa: D102 - trivial
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now += float(seconds)

    def set(self, seconds: float) -> None:
        """Set the clock to an absolute monotonic value."""
        self._now = float(seconds)


__all__ = ["ManualClock", "MonotonicClock"]
