"""Cohort retention — the N-day / N-week return triangle.

Each user's **cohort** is the bucket (day or week) of their *first* event. For
each cohort we then measure, for each subsequent period offset *n*, how many of
that cohort's users were active again in period *n*. The result is the classic
retention triangle plus convenience curves.

Two retention flavours are supported:

* **Classic (bounded)** — a user counts at offset *n* iff they had an event in
  exactly period *n*. This is the standard "Day-N retention" matrix.
* **Rolling (unbounded)** — a user counts at offset *n* iff they had any event in
  period *n* **or later**; monotone non-increasing, good for "are they still
  around" questions.

Pure; deterministic given the events and the chosen granularity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from app.analytics.events import TrackedEvent
from app.analytics.timebucket import Granularity, day_index, floor_to_bucket, week_index


@dataclass(frozen=True)
class CohortRow:
    """One cohort's retention across period offsets."""

    cohort_label: str
    cohort_start: datetime
    size: int
    # retained[n] = users active at offset n (retained[0] == size).
    retained: dict[int, int] = field(default_factory=dict)

    def rate(self, offset: int) -> float | None:
        """Retention fraction at ``offset`` (``None`` if cohort empty)."""
        if self.size == 0:
            return None
        return self.retained.get(offset, 0) / self.size


@dataclass(frozen=True)
class RetentionMatrix:
    """The full cohort-retention triangle + the pooled average curve."""

    granularity: Granularity
    max_offset: int
    cohorts: list[CohortRow]
    rolling: bool

    def average_curve(self) -> dict[int, float]:
        """Size-weighted average retention rate per offset across all cohorts."""
        totals: dict[int, int] = defaultdict(int)
        denom: dict[int, int] = defaultdict(int)
        for cohort in self.cohorts:
            for offset in range(self.max_offset + 1):
                # A cohort only contributes to an offset it could have reached.
                totals[offset] += cohort.retained.get(offset, 0)
                denom[offset] += cohort.size
        return {
            offset: (totals[offset] / denom[offset]) if denom[offset] else 0.0
            for offset in range(self.max_offset + 1)
        }


def _offset_fn(granularity: Granularity):  # type: ignore[no-untyped-def]
    if granularity is Granularity.WEEK:
        return week_index
    if granularity is Granularity.DAY:
        return day_index
    raise ValueError("retention supports DAY or WEEK granularity only")


def retention_matrix(
    events: list[TrackedEvent],
    *,
    granularity: Granularity = Granularity.DAY,
    max_offset: int = 7,
    rolling: bool = False,
) -> RetentionMatrix:
    """Build the cohort-retention triangle over ``events``.

    Args:
        events: the scrubbed population (anonymous users are skipped).
        granularity: ``DAY`` or ``WEEK`` cohorting/offset unit.
        max_offset: highest period offset to report (inclusive).
        rolling: if True, count "active at offset n or later"; else exactly n.
    """
    if max_offset < 0:
        raise ValueError("max_offset must be >= 0")
    offset_of = _offset_fn(granularity)

    # First-event time and the set of active period-offsets per user.
    first_seen: dict[str, datetime] = {}
    for event in events:
        if event.anon_user_id is None:
            continue
        existing = first_seen.get(event.anon_user_id)
        if existing is None or event.occurred_at < existing:
            first_seen[event.anon_user_id] = event.occurred_at

    # active_offsets[user] = set of offsets where the user had an event.
    active_offsets: dict[str, set[int]] = defaultdict(set)
    for event in events:
        user = event.anon_user_id
        if user is None:
            continue
        origin = first_seen[user]
        offset = offset_of(event.occurred_at, origin)
        if offset >= 0:
            active_offsets[user].add(offset)

    # Group users into cohorts by their first-seen bucket.
    cohorts_users: dict[datetime, list[str]] = defaultdict(list)
    for user, origin in first_seen.items():
        cohorts_users[floor_to_bucket(origin, granularity)].append(user)

    from app.analytics.timebucket import bucket_label

    rows: list[CohortRow] = []
    for cohort_start in sorted(cohorts_users):
        users = cohorts_users[cohort_start]
        retained: dict[int, int] = {}
        for offset in range(max_offset + 1):
            count = 0
            for user in users:
                offsets = active_offsets[user]
                if rolling:
                    if any(o >= offset for o in offsets):
                        count += 1
                else:
                    if offset in offsets:
                        count += 1
            retained[offset] = count
        rows.append(
            CohortRow(
                cohort_label=bucket_label(cohort_start, granularity),
                cohort_start=cohort_start,
                size=len(users),
                retained=retained,
            )
        )

    return RetentionMatrix(
        granularity=granularity,
        max_offset=max_offset,
        cohorts=rows,
        rolling=rolling,
    )


__all__ = ["CohortRow", "RetentionMatrix", "retention_matrix"]
