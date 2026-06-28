"""Rollup / aggregation jobs — fold raw events into summary rows.

Raw events are cheap to append but expensive to scan repeatedly. A *rollup* folds
a time window of events into a compact set of summary rows keyed by
``(bucket_start, granularity, dimension_key, metric)`` so a dashboard reads
pre-aggregated numbers instead of re-scanning the event log.

A :class:`RollupRow` is the canonical summary grain. :func:`compute_rollups`
produces the rows for a window (active-user counts per bucket — DAU/WAU/MAU
depending on granularity — plus per-event-name counts and a couple of engagement
aggregates). The rows are *idempotent*: recomputing a window yields the same rows,
so a Postgres upsert keyed on the grain is safe to re-run.

Pure given the events. The persistence (upsert into ``analytics_daily_rollup``)
lives in the repo/service; this module only computes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.analytics.events import READING_EVENTS, EventName, TrackedEvent
from app.analytics.timebucket import Granularity, bucket_label, floor_to_bucket

#: Reserved dimension-key for a metric that has no sub-dimension (e.g. DAU).
DIM_ALL = "all"


@dataclass(frozen=True)
class RollupRow:
    """One pre-aggregated summary value at the canonical grain."""

    bucket_start: datetime
    granularity: Granularity
    bucket_label: str
    dimension_key: str  # DIM_ALL, an event name, a book id, ...
    metric: str  # "active_users" | "events" | "reading_seconds" | "event:<name>" ...
    value: float

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The idempotency key (granularity, bucket_label, dimension_key, metric)."""
        return (self.granularity.value, self.bucket_label, self.dimension_key, self.metric)


# --------------------------------------------------------------------------- #
# Metric names produced by the standard rollup
# --------------------------------------------------------------------------- #

METRIC_ACTIVE_USERS = "active_users"
METRIC_EVENTS = "events"
METRIC_UNIQUE_BOOKS = "unique_books"
METRIC_UNIQUE_SESSIONS = "unique_sessions"
METRIC_READING_EVENTS = "reading_events"


def compute_rollups(
    events: list[TrackedEvent],
    *,
    granularity: Granularity = Granularity.DAY,
) -> list[RollupRow]:
    """Fold ``events`` into the standard summary rows at ``granularity``.

    Produces, per time bucket:

    * ``active_users`` (DIM_ALL) — distinct ``anon_user_id`` active in the bucket
      (this is DAU/WAU/MAU depending on the granularity passed).
    * ``events`` (DIM_ALL) — total events.
    * ``unique_books`` / ``unique_sessions`` (DIM_ALL).
    * ``reading_events`` (DIM_ALL) — events that are reading touches.
    * ``event:<name>`` (DIM_ALL) — per-event-name counts.

    Rows are returned sorted by ``(bucket_start, metric, dimension_key)`` for a
    stable, idempotent ordering.
    """
    # bucket -> aggregation scratch
    users: dict[datetime, set[str]] = defaultdict(set)
    books: dict[datetime, set[str]] = defaultdict(set)
    sessions: dict[datetime, set[str]] = defaultdict(set)
    totals: dict[datetime, int] = defaultdict(int)
    reading: dict[datetime, int] = defaultdict(int)
    per_name: dict[tuple[datetime, EventName], int] = defaultdict(int)

    for event in events:
        bucket = floor_to_bucket(event.occurred_at, granularity)
        totals[bucket] += 1
        if event.anon_user_id is not None:
            users[bucket].add(event.anon_user_id)
        if event.book_id is not None:
            books[bucket].add(event.book_id)
        if event.session_key is not None:
            sessions[bucket].add(event.session_key)
        if event.name in READING_EVENTS:
            reading[bucket] += 1
        per_name[(bucket, event.name)] += 1

    rows: list[RollupRow] = []

    def add(bucket: datetime, metric: str, dim: str, value: float) -> None:
        rows.append(
            RollupRow(
                bucket_start=bucket,
                granularity=granularity,
                bucket_label=bucket_label(bucket, granularity),
                dimension_key=dim,
                metric=metric,
                value=value,
            )
        )

    for bucket in sorted(totals):
        add(bucket, METRIC_ACTIVE_USERS, DIM_ALL, float(len(users[bucket])))
        add(bucket, METRIC_EVENTS, DIM_ALL, float(totals[bucket]))
        add(bucket, METRIC_UNIQUE_BOOKS, DIM_ALL, float(len(books[bucket])))
        add(bucket, METRIC_UNIQUE_SESSIONS, DIM_ALL, float(len(sessions[bucket])))
        add(bucket, METRIC_READING_EVENTS, DIM_ALL, float(reading[bucket]))

    for (bucket, name), count in per_name.items():
        add(bucket, f"event:{name.value}", DIM_ALL, float(count))

    rows.sort(key=lambda r: (r.bucket_start, r.metric, r.dimension_key))
    return rows


__all__ = [
    "DIM_ALL",
    "METRIC_ACTIVE_USERS",
    "METRIC_EVENTS",
    "METRIC_READING_EVENTS",
    "METRIC_UNIQUE_BOOKS",
    "METRIC_UNIQUE_SESSIONS",
    "RollupRow",
    "compute_rollups",
]
