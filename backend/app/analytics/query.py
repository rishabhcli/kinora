"""The flexible, time-bucketed analytics query layer.

A :class:`Query` describes *what to count, how to slice it, and how to bucket it
in time*; :func:`run_query` executes it over a list of events and returns a
:class:`QueryResult` of one series per group, each a list of
``(bucket_label, value)`` points on a dense (gap-filled) time axis.

Supported metrics:

* ``count`` — number of matching events.
* ``unique_users`` — distinct ``anon_user_id``.
* ``unique_books`` — distinct ``book_id``.
* ``unique_sessions`` — distinct ``session_key``.
* ``sum:<prop>`` / ``avg:<prop>`` — numeric aggregation over a prop.

Slicing:

* ``filters`` — name/book/user/session/prop-equality filters narrow the input.
* ``group_by`` — a dimension (``name`` / ``book_id`` / ``mode`` / ``prop:<key>``)
  splits the result into one series per distinct value.
* ``granularity`` — hour/day/week/month time buckets, dense over the window.

Pure (given the event list). The service layer feeds it events fetched from a
store; this module never does I/O.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.analytics.events import EventName, TrackedEvent
from app.analytics.timebucket import Granularity, bucket_label, bucket_range, floor_to_bucket


class Metric(enum.StrEnum):
    """Supported aggregate metrics (prop-parameterised ones use ``parse_metric``)."""

    COUNT = "count"
    UNIQUE_USERS = "unique_users"
    UNIQUE_BOOKS = "unique_books"
    UNIQUE_SESSIONS = "unique_sessions"
    SUM = "sum"  # sum:<prop>
    AVG = "avg"  # avg:<prop>


@dataclass(frozen=True)
class MetricSpec:
    """A parsed metric: the kind plus the optional prop it aggregates."""

    metric: Metric
    prop: str | None = None

    @property
    def label(self) -> str:
        return f"{self.metric.value}:{self.prop}" if self.prop else self.metric.value


def parse_metric(spec: str) -> MetricSpec:
    """Parse ``"count"`` / ``"unique_users"`` / ``"sum:dwell_ms"`` etc."""
    if ":" in spec:
        kind, _, prop = spec.partition(":")
        metric = Metric(kind)
        if metric not in (Metric.SUM, Metric.AVG):
            raise ValueError(f"metric {kind!r} does not take a prop argument")
        if not prop:
            raise ValueError(f"metric {kind!r} requires a prop, e.g. {kind}:dwell_ms")
        return MetricSpec(metric=metric, prop=prop)
    metric = Metric(spec)
    if metric in (Metric.SUM, Metric.AVG):
        raise ValueError(f"metric {spec!r} requires a prop, e.g. {spec}:dwell_ms")
    return MetricSpec(metric=metric)


@dataclass(frozen=True)
class Filters:
    """Narrowing predicate over the event population."""

    names: tuple[EventName, ...] | None = None
    book_id: str | None = None
    anon_user_id: str | None = None
    session_key: str | None = None
    prop_equals: dict[str, str] = field(default_factory=dict)

    def matches(self, event: TrackedEvent) -> bool:
        if self.names is not None and event.name not in self.names:
            return False
        if self.book_id is not None and event.book_id != self.book_id:
            return False
        if self.anon_user_id is not None and event.anon_user_id != self.anon_user_id:
            return False
        if self.session_key is not None and event.session_key != self.session_key:
            return False
        return all(event.prop_str(key) == value for key, value in self.prop_equals.items())


# --------------------------------------------------------------------------- #
# Group-by dimensions
# --------------------------------------------------------------------------- #

_NULL_GROUP = "∅"  # stable label for a missing dimension value


def _grouper(group_by: str | None) -> Callable[[TrackedEvent], str]:
    if group_by is None:
        return lambda _e: "all"
    if group_by == "name":
        return lambda e: e.name.value
    if group_by == "book_id":
        return lambda e: e.book_id or _NULL_GROUP
    if group_by == "mode":
        return lambda e: e.mode.value if e.mode else _NULL_GROUP
    if group_by == "anon_user_id":
        return lambda e: e.anon_user_id or _NULL_GROUP
    if group_by.startswith("prop:"):
        key = group_by[len("prop:") :]
        return lambda e: e.prop_str(key) or _NULL_GROUP
    raise ValueError(f"unsupported group_by dimension: {group_by!r}")


@dataclass(frozen=True)
class Query:
    """A complete analytics query."""

    metric: MetricSpec
    since: datetime
    until: datetime
    granularity: Granularity = Granularity.DAY
    filters: Filters = field(default_factory=Filters)
    group_by: str | None = None
    top_n: int | None = None  # keep only the N largest series (by total)


@dataclass(frozen=True)
class SeriesPoint:
    """One bucketed data point."""

    bucket: str
    value: float


@dataclass(frozen=True)
class Series:
    """One group's dense time series + its grand total."""

    group: str
    points: list[SeriesPoint]
    total: float


@dataclass(frozen=True)
class QueryResult:
    """The query output: the bucket axis labels + one :class:`Series` per group."""

    metric: str
    granularity: Granularity
    buckets: list[str]
    series: list[Series]


# --------------------------------------------------------------------------- #
# Per-bucket aggregation
# --------------------------------------------------------------------------- #


def _aggregate(metric: MetricSpec, events: list[TrackedEvent]) -> float:
    if metric.metric is Metric.COUNT:
        return float(len(events))
    if metric.metric is Metric.UNIQUE_USERS:
        return float(len({e.anon_user_id for e in events if e.anon_user_id is not None}))
    if metric.metric is Metric.UNIQUE_BOOKS:
        return float(len({e.book_id for e in events if e.book_id is not None}))
    if metric.metric is Metric.UNIQUE_SESSIONS:
        return float(len({e.session_key for e in events if e.session_key is not None}))
    # SUM / AVG over a prop
    assert metric.prop is not None
    values = [v for v in (e.prop_float(metric.prop) for e in events) if v is not None]
    if not values:
        return 0.0
    if metric.metric is Metric.SUM:
        return float(sum(values))
    return float(sum(values) / len(values))


def run_query(query: Query, events: list[TrackedEvent]) -> QueryResult:
    """Execute ``query`` over ``events`` and return a dense, grouped result."""
    axis = bucket_range(query.since, query.until, query.granularity)
    axis_labels = [bucket_label(b, query.granularity) for b in axis]
    grouper = _grouper(query.group_by)

    # bucketed[group][bucket_label] -> list of events
    bucketed: dict[str, dict[str, list[TrackedEvent]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for event in events:
        if event.occurred_at < query.since or event.occurred_at >= query.until:
            continue
        if not query.filters.matches(event):
            continue
        label = bucket_label(
            floor_to_bucket(event.occurred_at, query.granularity), query.granularity
        )
        bucketed[grouper(event)][label].append(event)

    series: list[Series] = []
    for group in bucketed:
        points: list[SeriesPoint] = []
        total = 0.0
        for label in axis_labels:
            value = _aggregate(query.metric, bucketed[group].get(label, []))
            points.append(SeriesPoint(bucket=label, value=value))
            total += value
        series.append(Series(group=group, points=points, total=total))

    # Stable order: by descending total, then group label.
    series.sort(key=lambda s: (-s.total, s.group))
    if query.top_n is not None:
        series = series[: query.top_n]

    return QueryResult(
        metric=query.metric.label,
        granularity=query.granularity,
        buckets=axis_labels,
        series=series,
    )


__all__ = [
    "Filters",
    "Metric",
    "MetricSpec",
    "Query",
    "QueryResult",
    "Series",
    "SeriesPoint",
    "parse_metric",
    "run_query",
]
