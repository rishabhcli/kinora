"""The executor — run a compiled plan and fold metric computations into rows.

:func:`execute_plan` is the second half of the pipeline (the compiler is the
first). It hands the plan's :class:`AggregationPlan` to a
:class:`~app.lakehouse.semantic.engine.QueryEngine`, then evaluates the
post-aggregation :class:`MetricComputation` list *in the order the compiler laid
them out* (dependency order), so each computation sees the columns it needs:

* :class:`MeasureProjection` — copy a base-aggregate column to its metric name.
* :class:`RatioComputation` — ``num / den`` per row (``den == 0 -> 0.0``).
* :class:`DerivedComputation` — evaluate the safe arithmetic AST per row.
* :class:`CumulativeComputation` — a running / trailing-window sum *per
  non-time group*, ordered by the time bucket (requires a time series).
* :class:`TimeComparisonComputation` — join each bucket against the bucket
  ``offset_periods`` earlier *within the same group* and report value/delta/%.

The result is a :class:`MetricResult`: typed columns + rows projected down to the
requested metrics (intermediate-only metrics are dropped from the output), with
the query's ordering + limit applied last.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.lakehouse.semantic.arith import compile_expr, evaluate
from app.lakehouse.semantic.engine import QueryEngine
from app.lakehouse.semantic.metrics import CalculationKind, WindowKind
from app.lakehouse.semantic.plan import (
    CumulativeComputation,
    DerivedComputation,
    MeasureProjection,
    QueryPlan,
    RatioComputation,
    TimeComparisonComputation,
)
from app.lakehouse.semantic.types import SortDirection


@dataclass(frozen=True, slots=True)
class MetricResult:
    """The final, user-facing result of a metric query."""

    dimensions: tuple[str, ...]
    time_column: str | None
    metrics: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def columns(self) -> tuple[str, ...]:
        cols: list[str] = list(self.dimensions)
        if self.time_column:
            cols.insert(0, self.time_column)
        cols.extend(self.metrics)
        return tuple(cols)

    def column(self, name: str) -> list[Any]:
        return [row.get(name) for row in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


def execute_plan(plan: QueryPlan, engine: QueryEngine) -> MetricResult:
    """Execute a compiled plan against an engine and return the final result."""
    agg = engine.execute_aggregation(plan.aggregation)
    # Work on mutable copies so computations can stack on each other.
    rows: list[dict[str, Any]] = [dict(r) for r in agg.rows]

    group_dims = plan.dimension_outputs
    time_col = plan.time_output

    for comp in plan.computations:
        if isinstance(comp, MeasureProjection):
            for row in rows:
                row[comp.metric] = row.get(comp.source_column)
        elif isinstance(comp, RatioComputation):
            for row in rows:
                row[comp.metric] = _ratio(row.get(comp.numerator), row.get(comp.denominator))
        elif isinstance(comp, DerivedComputation):
            ast = compile_expr(comp.expr)
            alias_to_metric = dict(comp.inputs)
            for row in rows:
                env = {
                    alias: _as_float(row.get(metric))
                    for alias, metric in alias_to_metric.items()
                }
                row[comp.metric] = evaluate(ast, env)
        elif isinstance(comp, CumulativeComputation):
            _apply_cumulative(rows, comp, group_dims, time_col)
        elif isinstance(comp, TimeComparisonComputation):
            _apply_time_comparison(rows, comp, group_dims, time_col)
        else:  # pragma: no cover - exhaustive union
            raise TypeError(f"unknown computation {comp!r}")

    projected = _project(rows, plan, group_dims, time_col)
    ordered = _order_and_limit(projected, plan)
    return MetricResult(
        dimensions=group_dims,
        time_column=time_col,
        metrics=plan.output_metrics,
        rows=tuple(ordered),
    )


# --------------------------------------------------------------------------- #
# Ratio / coercion helpers
# --------------------------------------------------------------------------- #


def _ratio(num: Any, den: Any) -> float:
    n = _as_float(num)
    d = _as_float(den)
    if n is None or d is None:
        return 0.0
    return n / d if d != 0 else 0.0


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"metric value {value!r} is not numeric")


# --------------------------------------------------------------------------- #
# Cumulative (running / trailing window) — per group, ordered by time
# --------------------------------------------------------------------------- #


def _group_signature(row: dict[str, Any], group_dims: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(d) for d in group_dims)


def _apply_cumulative(
    rows: list[dict[str, Any]],
    comp: CumulativeComputation,
    group_dims: Sequence[str],
    time_col: str | None,
) -> None:
    if time_col is None:
        raise ValueError(f"cumulative metric {comp.metric!r} requires a time series")
    per_group: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        per_group.setdefault(_group_signature(row, group_dims), []).append(row)
    for members in per_group.values():
        members.sort(key=lambda r: _time_key(r[time_col]))
        if comp.window is WindowKind.ALL_TIME:
            running = 0.0
            for row in members:
                running += _as_float(row.get(comp.base)) or 0.0
                row[comp.metric] = running
        else:  # TRAILING
            periods = comp.periods or 1
            for idx, row in enumerate(members):
                lo = max(0, idx - periods + 1)
                window = members[lo : idx + 1]
                row[comp.metric] = sum(
                    (_as_float(m.get(comp.base)) or 0.0) for m in window
                )


# --------------------------------------------------------------------------- #
# Time comparison (period over period) — per group, indexed by time bucket
# --------------------------------------------------------------------------- #


def _apply_time_comparison(
    rows: list[dict[str, Any]],
    comp: TimeComparisonComputation,
    group_dims: Sequence[str],
    time_col: str | None,
) -> None:
    if time_col is None:
        raise ValueError(f"time-comparison metric {comp.metric!r} requires a time series")
    # Index each group's rows by their *ordinal position* in the dense, sorted
    # time axis so "offset_periods earlier" is positional (one grain step == one
    # index step) regardless of calendar gaps in the data.
    per_group: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_group[_group_signature(row, group_dims)].append(row)
    for members in per_group.values():
        members.sort(key=lambda r: _time_key(r[time_col]))
        by_time = {_time_key(r[time_col]): r for r in members}
        sorted_keys = [k for k, _ in sorted(by_time.items())]
        index_of = {k: i for i, k in enumerate(sorted_keys)}
        for row in members:
            cur = _as_float(row.get(comp.base))
            key = _time_key(row[time_col])
            prior_idx = index_of[key] - comp.offset_periods
            prior = (
                _as_float(by_time[sorted_keys[prior_idx]].get(comp.base))
                if prior_idx >= 0
                else None
            )
            row[comp.metric] = _comparison_value(cur, prior, comp.calculation)


def _comparison_value(
    current: float | None, prior: float | None, calc: CalculationKind
) -> float | None:
    if calc is CalculationKind.VALUE:
        return prior
    if current is None or prior is None:
        return None
    if calc is CalculationKind.DELTA:
        return current - prior
    # PERCENT_CHANGE
    if prior == 0:
        return 0.0
    return (current - prior) / prior * 100.0


def _time_key(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(f"time bucket value {value!r} is not a datetime")


# --------------------------------------------------------------------------- #
# Projection + ordering
# --------------------------------------------------------------------------- #


def _project(
    rows: list[dict[str, Any]],
    plan: QueryPlan,
    group_dims: Sequence[str],
    time_col: str | None,
) -> list[dict[str, Any]]:
    keep = list(group_dims) + list(plan.output_metrics)
    if time_col:
        keep = [time_col, *keep]
    return [{k: row.get(k) for k in keep} for row in rows]


def _order_and_limit(rows: list[dict[str, Any]], plan: QueryPlan) -> list[dict[str, Any]]:
    ordered = rows
    if plan.order_by:
        # Stable multi-key sort: apply keys right-to-left.
        for ob in reversed(plan.order_by):
            key_name = ob.key
            ordered = sorted(
                ordered,
                key=lambda r: _sort_key(r.get(key_name)),
                reverse=ob.direction is SortDirection.DESC,
            )
    elif plan.time_output is not None:
        time_col = plan.time_output
        ordered = sorted(ordered, key=lambda r: _sort_key(r.get(time_col)))
    if plan.limit is not None:
        ordered = ordered[: plan.limit]
    return ordered


def _sort_key(value: Any) -> tuple[int, Any]:
    """A total order that pushes ``None`` last and keeps types comparable."""
    if value is None:
        return (1, 0)
    if isinstance(value, datetime):
        return (0, value.timestamp())
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (0, str(value))


__all__ = ["MetricResult", "execute_plan"]
