"""The compiled query-plan IR — the engine-agnostic execution contract.

The compiler lowers a :class:`~app.lakehouse.semantic.query.MetricQuery` into a
:class:`QueryPlan`: a flat, fully-resolved description of *the single grouped
aggregation* the warehouse must run, plus the *post-aggregation* metric
computations (ratios, derived expressions, cumulative/comparison passes) the
semantic layer evaluates on the aggregate rows.

Two-stage by design:

1. **AggregationPlan** — what the engine executes: a base model + join chain,
   the grouping keys (dimensions, optionally a time-truncated column), the set of
   :class:`AggExpr` aggregate columns (one per base measure, each carrying its
   own measure-level filter), the row filter, and the time window. This is the
   only part that touches data.
2. **MetricComputation** — pure arithmetic the layer folds over the aggregate
   rows to produce the requested metrics. No I/O.

The plan is frozen and carries a deterministic :pyattr:`fingerprint` so the
result cache and materialization advisor can key on it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.lakehouse.semantic.metrics import CalculationKind, WindowKind
from app.lakehouse.semantic.types import (
    Aggregation,
    FilterExpr,
    OrderBy,
    TimeGrain,
)


@dataclass(frozen=True, slots=True)
class JoinStep:
    """One edge of the resolved ``FROM`` chain (left-model -> right-model)."""

    left_model: str
    right_model: str
    left_key: str
    right_key: str
    join_type: str


@dataclass(frozen=True, slots=True)
class GroupKey:
    """A grouping column: a dimension expression, optionally time-truncated.

    ``output`` is the column name in the result; ``model``/``expr`` locate it in
    the source; ``grain`` (when set) means the engine must truncate the column to
    that time grain before grouping.
    """

    output: str
    model: str
    expr: str
    grain: TimeGrain | None = None
    is_time: bool = False


@dataclass(frozen=True, slots=True)
class AggExpr:
    """One aggregate column the engine computes (a base measure)."""

    output: str  # the column name the post-agg stage reads (the MeasureRef key)
    model: str
    expr: str
    agg: Aggregation
    measure_filter: FilterExpr | None = None


@dataclass(frozen=True, slots=True)
class AggregationPlan:
    """The single grouped aggregation handed to the engine / SQL renderer."""

    base_model: str
    base_source: str
    joins: tuple[JoinStep, ...]
    group_keys: tuple[GroupKey, ...]
    aggregates: tuple[AggExpr, ...]
    row_filter: FilterExpr | None
    time_window_filter: FilterExpr | None

    @property
    def dimension_outputs(self) -> tuple[str, ...]:
        return tuple(g.output for g in self.group_keys)

    @property
    def measure_outputs(self) -> tuple[str, ...]:
        return tuple(a.output for a in self.aggregates)


# --------------------------------------------------------------------------- #
# Post-aggregation metric computations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MeasureProjection:
    """A simple metric == a base-measure column, optionally already filtered.

    ``source_column`` is the aggregate column (a MeasureRef key) whose value
    becomes this metric. ``filtered`` marks that the metric's own filter was
    folded into a dedicated filtered aggregate column (distinct from the unfiltered
    measure), so two metrics over the same measure with different filters don't
    collide.
    """

    metric: str
    source_column: str


@dataclass(frozen=True, slots=True)
class RatioComputation:
    metric: str
    numerator: str  # an upstream metric name
    denominator: str


@dataclass(frozen=True, slots=True)
class DerivedComputation:
    metric: str
    expr: str
    inputs: tuple[tuple[str, str], ...]  # (alias, upstream metric name) pairs


@dataclass(frozen=True, slots=True)
class CumulativeComputation:
    metric: str
    base: str  # an upstream metric name (per-bucket value)
    window: WindowKind
    periods: int | None


@dataclass(frozen=True, slots=True)
class TimeComparisonComputation:
    metric: str
    base: str  # an upstream metric name (per-bucket value)
    offset_periods: int
    calculation: CalculationKind


#: The discriminated union of post-aggregation computations.
MetricComputation = (
    MeasureProjection
    | RatioComputation
    | DerivedComputation
    | CumulativeComputation
    | TimeComparisonComputation
)


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """The full compiled plan: an aggregation + ordered post-agg computations."""

    aggregation: AggregationPlan
    computations: tuple[MetricComputation, ...]
    output_metrics: tuple[str, ...]  # the metrics the *user* requested, in order
    dimension_outputs: tuple[str, ...]
    time_output: str | None  # the time bucket column name, if a time series
    time_grain: TimeGrain | None
    order_by: tuple[OrderBy, ...]
    limit: int | None

    @property
    def needs_time_ordering(self) -> bool:
        """Cumulative/comparison passes require buckets sorted by time per group."""
        return any(
            isinstance(c, (CumulativeComputation, TimeComparisonComputation))
            for c in self.computations
        )

    def fingerprint(self) -> str:
        """A stable hex digest of the plan (cache key, advisor key, lineage id)."""
        return hashlib.sha256(_canonical(self).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Canonical serialisation (for the fingerprint) — order-stable, type-stable.
# --------------------------------------------------------------------------- #


def _canonical(plan: QueryPlan) -> str:
    return json.dumps(_plan_to_jsonable(plan), sort_keys=True, separators=(",", ":"))


def _plan_to_jsonable(plan: QueryPlan) -> dict[str, Any]:
    agg = plan.aggregation
    return {
        "base_model": agg.base_model,
        "base_source": agg.base_source,
        "joins": [
            [j.left_model, j.right_model, j.left_key, j.right_key, j.join_type]
            for j in agg.joins
        ],
        "group_keys": [
            [g.output, g.model, g.expr, g.grain.value if g.grain else None, g.is_time]
            for g in agg.group_keys
        ],
        "aggregates": [
            [a.output, a.model, a.expr, a.agg.value, _filter_repr(a.measure_filter)]
            for a in agg.aggregates
        ],
        "row_filter": _filter_repr(agg.row_filter),
        "time_window": _filter_repr(agg.time_window_filter),
        "computations": [_computation_repr(c) for c in plan.computations],
        "output_metrics": list(plan.output_metrics),
        "dimension_outputs": list(plan.dimension_outputs),
        "time_output": plan.time_output,
        "time_grain": plan.time_grain.value if plan.time_grain else None,
        "order_by": [[o.key, o.direction.value] for o in plan.order_by],
        "limit": plan.limit,
    }


def _filter_repr(expr: FilterExpr | None) -> Any:
    # repr() of frozen dataclasses is stable and total, and the fingerprint only
    # needs *a* deterministic encoding, not a round-trippable one.
    return None if expr is None else repr(expr)


def _computation_repr(comp: MetricComputation) -> list[Any]:
    if isinstance(comp, MeasureProjection):
        return ["proj", comp.metric, comp.source_column]
    if isinstance(comp, RatioComputation):
        return ["ratio", comp.metric, comp.numerator, comp.denominator]
    if isinstance(comp, DerivedComputation):
        return ["derived", comp.metric, comp.expr, [list(p) for p in comp.inputs]]
    if isinstance(comp, CumulativeComputation):
        return ["cumul", comp.metric, comp.base, comp.window.value, comp.periods]
    if isinstance(comp, TimeComparisonComputation):
        return [
            "tcomp",
            comp.metric,
            comp.base,
            comp.offset_periods,
            comp.calculation.value,
        ]
    raise TypeError(f"unknown computation {comp!r}")  # pragma: no cover


__all__ = [
    "AggExpr",
    "AggregationPlan",
    "CumulativeComputation",
    "DerivedComputation",
    "GroupKey",
    "JoinStep",
    "MeasureProjection",
    "MetricComputation",
    "QueryPlan",
    "RatioComputation",
    "TimeComparisonComputation",
]
