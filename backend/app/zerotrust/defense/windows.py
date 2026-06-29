"""Sliding-window counters for rate-based detection.

Rate detectors need to answer "how many events from this key in the last N
seconds" and "how many *distinct* values has this key touched" cheaply and
without unbounded memory. Two structures cover that:

* :class:`SlidingCounter` — an exact event count over a trailing time window,
  backed by a deque of timestamps it prunes lazily on each touch; and
* :class:`DistinctWindow` — an approximate distinct-count of values seen within a
  window (e.g. distinct usernames a single ip tried), with a hard cap so a
  pathological attacker can't blow up memory.

Both are time-driven through an injected monotonic ``now`` value rather than a
clock object, so a detector that already holds a clock passes ``clock.mono()``
in — keeping these structures pure and trivially testable.
"""

from __future__ import annotations

from collections import deque


class SlidingCounter:
    """Exact count of events within a trailing ``window`` seconds.

    Stores one timestamp per event in a monotonic deque and prunes anything
    older than ``now - window`` on every ``hit``/``count`` call, so the count is
    always current and memory is bounded by the in-window event count.
    """

    __slots__ = ("window", "_ts")

    def __init__(self, window: float) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self.window = float(window)
        self._ts: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        ts = self._ts
        while ts and ts[0] <= cutoff:
            ts.popleft()

    def hit(self, now: float, weight: int = 1) -> int:
        """Record ``weight`` events at ``now`` and return the current count."""
        for _ in range(max(1, weight)):
            self._ts.append(now)
        self._prune(now)
        return len(self._ts)

    def count(self, now: float) -> int:
        self._prune(now)
        return len(self._ts)

    def rate_per_sec(self, now: float) -> float:
        """Count divided by the window length (events/second)."""
        return self.count(now) / self.window


class DistinctWindow:
    """Approximate count of distinct values seen within a trailing window.

    Keeps each value's most-recent timestamp in an insertion-ordered map and
    prunes expired entries on touch. A ``cap`` bounds memory: once the live set
    hits the cap, new distinct values still increment a saturating counter so the
    detector sees "very many" without the map growing without bound.
    """

    __slots__ = ("window", "cap", "_last_seen", "_overflow")

    def __init__(self, window: float, cap: int = 4096) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        if cap < 1:
            raise ValueError("cap must be >= 1")
        self.window = float(window)
        self.cap = cap
        self._last_seen: dict[str, float] = {}
        self._overflow = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        expired = [k for k, t in self._last_seen.items() if t <= cutoff]
        for k in expired:
            del self._last_seen[k]
        if not self._last_seen:
            self._overflow = 0

    def add(self, value: str, now: float) -> int:
        """Record ``value`` at ``now`` and return the current distinct count."""
        self._prune(now)
        if value in self._last_seen or len(self._last_seen) < self.cap:
            self._last_seen[value] = now
        else:
            self._overflow += 1
        return self.count(now)

    def count(self, now: float) -> int:
        self._prune(now)
        return len(self._last_seen) + self._overflow
