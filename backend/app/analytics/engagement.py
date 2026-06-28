"""Reading-engagement metrics aggregated over derived reading sessions.

Where :mod:`app.analytics.sessionize` produces *per-session* engagement,
this module folds many sessions into the population-level numbers a product
dashboard shows (kinora.md §5.3 — the reading experience this whole product is
about): median pages/min, completion-rate distribution, drop-off histogram,
viewer-vs-director split, stall rate, and time-on-book.

Pure aggregation over :class:`ReadingSession` lists — no I/O, deterministic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from app.analytics.sessionize import ReadingSession


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def _mean_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class EngagementSummary:
    """Population-level reading-engagement metrics over a set of sessions."""

    session_count: int
    unique_readers: int
    unique_books: int
    total_reading_seconds: float
    median_session_seconds: float | None
    median_pages_per_min: float | None
    mean_pages_per_min: float | None
    median_words_per_min: float | None
    mean_completion_ratio: float | None
    completion_rate: float | None  # fraction of sessions that reached >= 90%
    director_session_rate: float | None  # fraction using Director mode
    stall_rate: float | None  # mean stalls per session
    dropoff_histogram: dict[int, int] = field(default_factory=dict)
    completion_buckets: dict[str, int] = field(default_factory=dict)


#: A session counts as "completed" at/above this completion ratio.
COMPLETION_THRESHOLD = 0.9

#: Completion buckets for the distribution chart (lower-bound labels).
_COMPLETION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-25%", 0.0, 0.25),
    ("25-50%", 0.25, 0.50),
    ("50-75%", 0.50, 0.75),
    ("75-90%", 0.75, 0.90),
    ("90-100%", 0.90, 1.0001),
)


def _completion_bucket(ratio: float) -> str:
    for label, lo, hi in _COMPLETION_BUCKETS:
        if lo <= ratio < hi:
            return label
    return _COMPLETION_BUCKETS[-1][0]


def summarize_engagement(sessions: list[ReadingSession]) -> EngagementSummary:
    """Fold a list of reading sessions into an :class:`EngagementSummary`."""
    if not sessions:
        return EngagementSummary(
            session_count=0,
            unique_readers=0,
            unique_books=0,
            total_reading_seconds=0.0,
            median_session_seconds=None,
            median_pages_per_min=None,
            mean_pages_per_min=None,
            median_words_per_min=None,
            mean_completion_ratio=None,
            completion_rate=None,
            director_session_rate=None,
            stall_rate=None,
        )

    durations = [s.duration_s for s in sessions]
    ppm = [s.pages_per_min for s in sessions if s.pages_per_min is not None]
    wpm = [s.words_per_min for s in sessions if s.words_per_min is not None]
    completions = [s.completion_ratio for s in sessions if s.completion_ratio is not None]

    dropoff_hist: Counter[int] = Counter()
    for s in sessions:
        if s.dropoff_page is not None:
            dropoff_hist[s.dropoff_page] += 1

    completion_buckets: Counter[str] = Counter()
    for ratio in completions:
        completion_buckets[_completion_bucket(ratio)] += 1

    completed = sum(1 for r in completions if r >= COMPLETION_THRESHOLD)
    completion_rate = (completed / len(completions)) if completions else None
    director_rate = sum(1 for s in sessions if s.is_director_session) / len(sessions)
    stall_rate = sum(s.stall_count for s in sessions) / len(sessions)

    readers = {s.anon_user_id for s in sessions if s.anon_user_id is not None}
    books = {s.book_id for s in sessions if s.book_id is not None}

    return EngagementSummary(
        session_count=len(sessions),
        unique_readers=len(readers),
        unique_books=len(books),
        total_reading_seconds=sum(durations),
        median_session_seconds=_median_or_none(durations),
        median_pages_per_min=_median_or_none(ppm),
        mean_pages_per_min=_mean_or_none(ppm),
        median_words_per_min=_median_or_none(wpm),
        mean_completion_ratio=_mean_or_none(completions),
        completion_rate=completion_rate,
        director_session_rate=director_rate,
        stall_rate=stall_rate,
        dropoff_histogram=dict(dropoff_hist),
        completion_buckets=dict(completion_buckets),
    )


__all__ = [
    "COMPLETION_THRESHOLD",
    "EngagementSummary",
    "summarize_engagement",
]
