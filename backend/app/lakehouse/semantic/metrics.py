"""The metric-definition language — *metrics as code*.

A **metric** is a named, governable, lineage-tracked computation built on top of
the semantic model's measures. The language mirrors dbt-MetricFlow's metric
kinds, each a frozen dataclass with a discriminating ``kind``:

* :class:`SimpleMetric` — one measure, optionally filtered (the leaf metric).
* :class:`RatioMetric` — ``numerator / denominator`` of two metrics.
* :class:`DerivedMetric` — an arithmetic expression over named input metrics
  (``alias -> metric``), e.g. ``(1 - rejected / total) * 100``.
* :class:`CumulativeMetric` — a running / windowed accumulation of a base metric
  over the time grain (all-time, or a trailing window of N grains).
* :class:`TimeComparisonMetric` — a base metric versus itself shifted back by an
  offset (period-over-period, with ``value`` / ``delta`` / ``percent_change``
  calculations).

Metrics reference other metrics *by name*, so the registry resolves them into a
DAG (cycles are rejected) and the compiler expands a metric into the set of base
measures it ultimately needs. Filters declared on a metric narrow only that
metric (and everything derived from it), independent of the query's filters.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.lakehouse.semantic.types import (
    FilterExpr,
    validate_identifier,
)


class MetricKind(enum.StrEnum):
    SIMPLE = "simple"
    RATIO = "ratio"
    DERIVED = "derived"
    CUMULATIVE = "cumulative"
    TIME_COMPARISON = "time_comparison"


class CalculationKind(enum.StrEnum):
    """How a time-comparison metric reports the shifted-vs-current relationship."""

    VALUE = "value"  # the offset (prior-period) value itself
    DELTA = "delta"  # current - prior
    PERCENT_CHANGE = "percent_change"  # (current - prior) / prior * 100


class WindowKind(enum.StrEnum):
    """A cumulative metric's accumulation window."""

    ALL_TIME = "all_time"  # running total from the start of the series
    TRAILING = "trailing"  # trailing N grains (a moving window)


@dataclass(frozen=True, slots=True)
class _MetricBase:
    """Fields shared by every metric kind (name + presentation + grain hint)."""

    name: str
    label: str | None = None
    description: str = ""
    #: An optional format hint for the catalog/UI (``"percent"``, ``"usd"``, …).
    format: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.name, what="metric name")

    @property
    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class SimpleMetric(_MetricBase):
    """A single measure, optionally filtered — the leaf of every metric DAG.

    ``measure`` is the measure name; ``model`` optionally pins which model owns it
    (else the registry resolves it uniquely across models). ``metric_filter``
    narrows this metric only.
    """

    measure: str = ""
    model: str | None = None
    metric_filter: FilterExpr | None = None
    kind: MetricKind = field(default=MetricKind.SIMPLE, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.measure:
            raise ValueError(f"simple metric {self.name!r} requires a measure")
        validate_identifier(self.measure, what="measure reference")
        if self.model is not None:
            validate_identifier(self.model, what="model reference")


@dataclass(frozen=True, slots=True)
class RatioMetric(_MetricBase):
    """``numerator / denominator`` over two named metrics (0/0 -> 0)."""

    numerator: str = ""
    denominator: str = ""
    kind: MetricKind = field(default=MetricKind.RATIO, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.numerator or not self.denominator:
            raise ValueError(f"ratio metric {self.name!r} requires numerator + denominator")
        validate_identifier(self.numerator, what="ratio numerator")
        validate_identifier(self.denominator, what="ratio denominator")


@dataclass(frozen=True, slots=True)
class DerivedMetric(_MetricBase):
    """An arithmetic expression over aliased input metrics.

    ``expr`` is a restricted arithmetic expression (``+ - * /``, parentheses,
    numeric literals, and the alias names from ``inputs``). ``inputs`` maps each
    alias used in ``expr`` to the metric name it refers to. The compiler computes
    every input metric and the evaluator folds them through a tiny, safe
    arithmetic interpreter (no Python ``eval``).
    """

    expr: str = ""
    inputs: Mapping[str, str] = field(default_factory=dict)
    kind: MetricKind = field(default=MetricKind.DERIVED, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.expr:
            raise ValueError(f"derived metric {self.name!r} requires an expr")
        if not self.inputs:
            raise ValueError(f"derived metric {self.name!r} requires at least one input")
        for alias, target in self.inputs.items():
            validate_identifier(alias, what="derived input alias")
            validate_identifier(target, what="derived input metric")
        # Freeze the mapping so the dataclass stays hashable.
        object.__setattr__(self, "inputs", dict(self.inputs))


@dataclass(frozen=True, slots=True)
class CumulativeMetric(_MetricBase):
    """A running or trailing-window accumulation of a base metric over time.

    ``window`` selects all-time running total vs a trailing window; ``periods``
    is the trailing window length in *grains* (required for ``TRAILING``). The
    accumulated base metric must be additive (the registry/compiler enforces it)
    so partial sums roll forward correctly.
    """

    base: str = ""
    window: WindowKind = WindowKind.ALL_TIME
    periods: int | None = None
    kind: MetricKind = field(default=MetricKind.CUMULATIVE, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.base:
            raise ValueError(f"cumulative metric {self.name!r} requires a base metric")
        validate_identifier(self.base, what="cumulative base")
        if self.window is WindowKind.TRAILING:
            if self.periods is None or self.periods < 1:
                raise ValueError(
                    f"cumulative metric {self.name!r}: trailing window needs periods >= 1"
                )
        elif self.periods is not None:
            raise ValueError(
                f"cumulative metric {self.name!r}: all_time window takes no periods"
            )


@dataclass(frozen=True, slots=True)
class TimeComparisonMetric(_MetricBase):
    """A base metric versus itself shifted back ``offset_periods`` *grains*.

    With ``calculation=PERCENT_CHANGE`` and ``offset_periods=1`` this is the
    classic period-over-period growth. The offset is expressed in whole grains so
    a query at ``grain=month, offset=1`` is month-over-month; the same metric at
    ``grain=year`` becomes year-over-year for free.
    """

    base: str = ""
    offset_periods: int = 1
    calculation: CalculationKind = CalculationKind.PERCENT_CHANGE
    kind: MetricKind = field(default=MetricKind.TIME_COMPARISON, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.base:
            raise ValueError(f"time-comparison metric {self.name!r} requires a base metric")
        validate_identifier(self.base, what="time-comparison base")
        if self.offset_periods < 1:
            raise ValueError(
                f"time-comparison metric {self.name!r}: offset_periods must be >= 1"
            )


#: The discriminated union of all metric kinds.
Metric = (
    SimpleMetric | RatioMetric | DerivedMetric | CumulativeMetric | TimeComparisonMetric
)


def metric_dependencies(metric: Metric) -> tuple[str, ...]:
    """Return the names of the metrics ``metric`` directly references (DAG edges)."""
    if isinstance(metric, SimpleMetric):
        return ()
    if isinstance(metric, RatioMetric):
        return (metric.numerator, metric.denominator)
    if isinstance(metric, DerivedMetric):
        # Preserve declared order, dedupe.
        seen: dict[str, None] = {}
        for target in metric.inputs.values():
            seen.setdefault(target, None)
        return tuple(seen)
    if isinstance(metric, CumulativeMetric):
        return (metric.base,)
    if isinstance(metric, TimeComparisonMetric):
        return (metric.base,)
    raise TypeError(f"unknown metric kind: {metric!r}")  # pragma: no cover


def requires_time(metric: Metric) -> bool:
    """True if a metric is meaningless without a time grain (cumulative/comparison)."""
    return isinstance(metric, (CumulativeMetric, TimeComparisonMetric))


__all__ = [
    "CalculationKind",
    "CumulativeMetric",
    "DerivedMetric",
    "Metric",
    "MetricKind",
    "RatioMetric",
    "SimpleMetric",
    "TimeComparisonMetric",
    "WindowKind",
    "metric_dependencies",
    "requires_time",
]
