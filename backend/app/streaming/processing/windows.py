"""Event-time window assigners: tumbling, sliding, and session.

A *window assigner* maps a record's event-time to the set of windows it belongs
to. The three classic shapes, all here:

* **Tumbling** — fixed-size, non-overlapping. Each record lands in exactly one
  window. The natural choice for "events per 10 seconds".
* **Sliding** — fixed-size, overlapping by a slide step. Each record lands in
  ``size / slide`` windows. The choice for a moving average that updates every
  few seconds over a longer horizon.
* **Session** — data-driven, gap-based. A window covers a burst of activity; a
  gap of inactivity longer than ``gap_ms`` starts a new session. Adjacent or
  overlapping sessions **merge**. The natural model for a *reading session* —
  exactly Kinora's idle-pause concept (§4.7).

Windows are half-open intervals ``[start, end)`` in epoch ms. ``max_timestamp``
is ``end - 1`` (the last instant the window covers); a window fires when the
watermark passes ``end - 1 + allowed_lateness``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, order=True)
class TimeWindow:
    """A half-open event-time interval ``[start, end)`` in epoch ms."""

    start: int
    end: int

    @property
    def max_timestamp(self) -> int:
        """The last instant this window covers (``end - 1``)."""

        return self.end - 1

    def intersects(self, other: TimeWindow) -> bool:
        return self.start <= other.end and other.start <= self.end

    def cover(self, other: TimeWindow) -> TimeWindow:
        """The smallest window covering both (used when merging sessions)."""

        return TimeWindow(min(self.start, other.start), max(self.end, other.end))


class WindowAssigner(Protocol):
    """Maps an event timestamp to the windows it belongs to."""

    def assign(self, timestamp: int) -> list[TimeWindow]: ...

    @property
    def is_merging(self) -> bool:
        """Whether windows can merge after assignment (session windows do)."""
        ...


def _window_start(timestamp: int, offset: int, size: int) -> int:
    """Align ``timestamp`` down to the nearest window boundary (Flink's rule)."""

    return timestamp - (timestamp - offset) % size


@dataclass(frozen=True, slots=True)
class TumblingEventTimeWindows:
    """Fixed-size, non-overlapping windows of ``size_ms``."""

    size_ms: int
    offset_ms: int = 0

    def __post_init__(self) -> None:
        if self.size_ms <= 0:
            raise ValueError("tumbling window size must be > 0")

    def assign(self, timestamp: int) -> list[TimeWindow]:
        start = _window_start(timestamp, self.offset_ms, self.size_ms)
        return [TimeWindow(start, start + self.size_ms)]

    @property
    def is_merging(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SlidingEventTimeWindows:
    """Fixed-size windows of ``size_ms`` advancing by ``slide_ms``.

    Each record belongs to ``ceil(size / slide)`` windows. ``size`` need not be a
    multiple of ``slide``; the assigner walks back from the record's aligned
    pane to enumerate every covering window.
    """

    size_ms: int
    slide_ms: int
    offset_ms: int = 0

    def __post_init__(self) -> None:
        if self.size_ms <= 0 or self.slide_ms <= 0:
            raise ValueError("sliding window size and slide must be > 0")

    def assign(self, timestamp: int) -> list[TimeWindow]:
        last_start = _window_start(timestamp, self.offset_ms, self.slide_ms)
        windows: list[TimeWindow] = []
        start = last_start
        while start > timestamp - self.size_ms:
            windows.append(TimeWindow(start, start + self.size_ms))
            start -= self.slide_ms
        windows.sort()
        return windows

    @property
    def is_merging(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SessionWindows:
    """Gap-based, mergeable windows.

    On assignment each record gets a provisional window ``[ts, ts + gap)``; the
    window operator then merges any windows that touch or overlap, so a run of
    records spaced less than ``gap_ms`` apart collapses into one session window.
    A gap ``>= gap_ms`` of silence ends the session.
    """

    gap_ms: int

    def __post_init__(self) -> None:
        if self.gap_ms <= 0:
            raise ValueError("session gap must be > 0")

    def assign(self, timestamp: int) -> list[TimeWindow]:
        return [TimeWindow(timestamp, timestamp + self.gap_ms)]

    @property
    def is_merging(self) -> bool:
        return True


def merge_windows(windows: list[TimeWindow]) -> list[tuple[TimeWindow, list[TimeWindow]]]:
    """Merge overlapping/adjacent windows (the session-merge primitive).

    Returns a list of ``(merged_window, [originals...])`` so the operator can
    fold the state of every original into the merged target. Pure and
    deterministic: input is sorted, intervals that touch are coalesced.
    """

    if not windows:
        return []
    ordered = sorted(set(windows))
    merged: list[tuple[TimeWindow, list[TimeWindow]]] = []
    current = ordered[0]
    members = [ordered[0]]
    for win in ordered[1:]:
        if win.start <= current.end:  # touching or overlapping -> merge
            current = current.cover(win)
            members.append(win)
        else:
            merged.append((current, members))
            current = win
            members = [win]
    merged.append((current, members))
    return merged
