"""The self-serve *query request* — what a caller asks the semantic layer for.

A :class:`MetricQuery` is the public, ergonomic request object: a list of metric
names, the dimensions to group by, optional filters, an optional time grain +
window, ordering, and a limit. It is deliberately *engine-agnostic and
declarative* — it names metrics and dimensions, never tables or SQL — so the
same request runs against the in-memory engine in a test and the warehouse in
production unchanged.

The request is validated lightly here (well-formedness only); semantic
validation against the model/metric registry happens in the compiler, which has
the graph in hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.lakehouse.semantic.types import (
    FieldRef,
    FilterExpr,
    OrderBy,
    TimeGrain,
    parse_field_ref,
)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A half-open ``[start, end)`` filter on the query's time dimension."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")


@dataclass(frozen=True, slots=True)
class MetricQuery:
    """A self-serve metrics request.

    ``metrics`` — the metric names to compute (order preserved in the output).
    ``group_by`` — dimension field refs to slice by (entity-qualified or bare).
    ``time_grain`` — when set, the query is time-bucketed at this grain and the
        chosen time dimension is added as an implicit leading group-by column.
    ``time_dimension`` — which time dimension to bucket on (defaults to the
        primary model's first time dimension when omitted).
    ``time_window`` — restricts the time dimension to ``[start, end)``.
    ``filters`` — additional filter expressions (conjoined).
    ``order_by`` / ``limit`` — output shaping.
    """

    metrics: tuple[str, ...]
    group_by: tuple[FieldRef, ...] = ()
    time_grain: TimeGrain | None = None
    time_dimension: FieldRef | None = None
    time_window: TimeWindow | None = None
    filters: tuple[FilterExpr, ...] = ()
    order_by: tuple[OrderBy, ...] = ()
    limit: int | None = None

    def __post_init__(self) -> None:
        if not self.metrics:
            raise ValueError("a metric query must request at least one metric")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError(f"duplicate metrics in query: {self.metrics}")
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be non-negative")
        if self.time_dimension is not None and self.time_grain is None:
            raise ValueError("time_dimension requires a time_grain")
        if self.time_window is not None and self.time_grain is None:
            raise ValueError("time_window requires a time_grain")

    @property
    def is_time_series(self) -> bool:
        return self.time_grain is not None

    @classmethod
    def of(
        cls,
        *metrics: str,
        group_by: tuple[str, ...] = (),
        time_grain: TimeGrain | None = None,
        time_dimension: str | None = None,
        time_window: TimeWindow | None = None,
        filters: tuple[FilterExpr, ...] = (),
        order_by: tuple[OrderBy, ...] = (),
        limit: int | None = None,
    ) -> MetricQuery:
        """Ergonomic builder taking *string* dimension/time names (parsed to refs)."""
        return cls(
            metrics=tuple(metrics),
            group_by=tuple(parse_field_ref(g) for g in group_by),
            time_grain=time_grain,
            time_dimension=parse_field_ref(time_dimension) if time_dimension else None,
            time_window=time_window,
            filters=filters,
            order_by=order_by,
            limit=limit,
        )


__all__ = ["MetricQuery", "TimeWindow"]
