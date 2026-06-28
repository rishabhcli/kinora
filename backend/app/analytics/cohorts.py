"""Cohort assignment and per-cohort metric rollups.

A *cohort* groups users by a stable attribute fixed at acquisition time, so you
can compare groups apples-to-apples (e.g. "did readers who arrived via the
public-domain catalogue read more than uploaders?"). This module provides the
common cohorting strategies and a generic per-cohort metric aggregator.

Cohorting strategies:

* **signup_period** — the day/week of a user's first event.
* **acquisition_source** — the ``source`` prop on the user's first
  ``book.added`` event (``upload`` / ``public_domain`` / ``demo``).
* **first_book** — the ``book_id`` of the first book the user opened.
* **platform** — the ``platform`` prop on the user's first event.

Each strategy returns ``{cohort_label: {user_id, ...}}``. :func:`cohort_metrics`
then folds any metric function over each cohort's users' events.

Pure; deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from app.analytics.events import EventName, TrackedEvent
from app.analytics.timebucket import Granularity, bucket_label, floor_to_bucket

#: A mapping cohort-label -> set of anon_user_ids.
CohortAssignment = dict[str, set[str]]


def _first_event_per_user(events: list[TrackedEvent]) -> dict[str, TrackedEvent]:
    first: dict[str, TrackedEvent] = {}
    for event in sorted(events, key=lambda e: (e.occurred_at, e.event_id)):
        if event.anon_user_id is None:
            continue
        first.setdefault(event.anon_user_id, event)
    return first


def cohort_by_signup_period(
    events: list[TrackedEvent], *, granularity: Granularity = Granularity.WEEK
) -> CohortAssignment:
    """Cohort users by the bucket of their first event (signup period)."""
    out: CohortAssignment = defaultdict(set)
    for user, first in _first_event_per_user(events).items():
        label = bucket_label(floor_to_bucket(first.occurred_at, granularity), granularity)
        out[label].add(user)
    return dict(out)


def cohort_by_first_prop(
    events: list[TrackedEvent],
    *,
    trigger: EventName,
    prop: str,
    unknown_label: str = "unknown",
) -> CohortAssignment:
    """Cohort users by a categorical ``prop`` on their first ``trigger`` event.

    Used for ``acquisition_source`` (trigger ``book.added``, prop ``source``) and
    similar. A user with no ``trigger`` event is not assigned to any cohort.
    """
    by_user: dict[str, list[TrackedEvent]] = defaultdict(list)
    for event in events:
        if event.anon_user_id is None or event.name != trigger:
            continue
        by_user[event.anon_user_id].append(event)

    out: CohortAssignment = defaultdict(set)
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.occurred_at, e.event_id))
        value = user_events[0].prop_str(prop) or unknown_label
        out[value].add(user)
    return dict(out)


def cohort_by_platform(events: list[TrackedEvent]) -> CohortAssignment:
    """Cohort users by the ``platform`` prop on their first event."""
    out: CohortAssignment = defaultdict(set)
    for user, first in _first_event_per_user(events).items():
        out[first.prop_str("platform") or "unknown"].add(user)
    return dict(out)


def cohort_by_first_book(events: list[TrackedEvent]) -> CohortAssignment:
    """Cohort users by the ``book_id`` of their first ``book.opened`` event."""
    by_user: dict[str, list[TrackedEvent]] = defaultdict(list)
    for event in events:
        if event.anon_user_id is None or event.name != EventName.BOOK_OPENED:
            continue
        by_user[event.anon_user_id].append(event)
    out: CohortAssignment = defaultdict(set)
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.occurred_at, e.event_id))
        book = user_events[0].book_id or "unknown"
        out[book].add(user)
    return dict(out)


@dataclass(frozen=True)
class CohortMetric:
    """A single cohort's size and computed metric value."""

    label: str
    size: int
    value: float


#: A metric function maps a cohort's users' events -> a scalar.
MetricFn = Callable[[list[TrackedEvent]], float]


def cohort_metrics(
    events: list[TrackedEvent],
    assignment: CohortAssignment,
    metric: MetricFn,
) -> list[CohortMetric]:
    """Apply ``metric`` to each cohort's slice of ``events``; return sorted rows.

    Each cohort's slice is the events whose ``anon_user_id`` is in that cohort.
    Rows are returned sorted by descending metric value (ties by label) so the
    "best" cohort is first.
    """
    users_to_events: dict[str, list[TrackedEvent]] = defaultdict(list)
    for event in events:
        if event.anon_user_id is not None:
            users_to_events[event.anon_user_id].append(event)

    rows: list[CohortMetric] = []
    for label, users in assignment.items():
        slice_events: list[TrackedEvent] = []
        for user in users:
            slice_events.extend(users_to_events.get(user, []))
        rows.append(CohortMetric(label=label, size=len(users), value=metric(slice_events)))
    rows.sort(key=lambda r: (-r.value, r.label))
    return rows


# --------------------------------------------------------------------------- #
# A few ready-made metric functions
# --------------------------------------------------------------------------- #


def metric_event_count(events: list[TrackedEvent]) -> float:
    """Total events (a raw activity metric)."""
    return float(len(events))


def metric_events_per_user(events: list[TrackedEvent]) -> float:
    """Mean events per distinct user in the slice."""
    users = {e.anon_user_id for e in events if e.anon_user_id is not None}
    return (len(events) / len(users)) if users else 0.0


def metric_reading_event_share(events: list[TrackedEvent]) -> float:
    """Fraction of the slice's events that are reading touches (0..1)."""
    from app.analytics.events import READING_EVENTS

    if not events:
        return 0.0
    reading = sum(1 for e in events if e.name in READING_EVENTS)
    return reading / len(events)


__all__ = [
    "CohortAssignment",
    "CohortMetric",
    "MetricFn",
    "cohort_by_first_book",
    "cohort_by_first_prop",
    "cohort_by_platform",
    "cohort_by_signup_period",
    "cohort_metrics",
    "metric_event_count",
    "metric_events_per_user",
    "metric_reading_event_share",
]
