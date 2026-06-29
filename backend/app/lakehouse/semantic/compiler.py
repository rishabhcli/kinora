"""The compiler — lower a :class:`MetricQuery` into a :class:`QueryPlan`.

This is the deterministic heart of the layer. Given a validated
:class:`~app.lakehouse.semantic.registry.SemanticGraph`, it:

1. resolves every requested metric to its transitive base measures and picks a
   **base model** (the model the dimensions/time live on);
2. resolves the **join chain** from the base model to every model a base measure
   lives on (many-to-one only, so additive aggregation stays fan-out-safe);
3. validates the **group-by dimensions** and the **time dimension/grain** against
   the models, rejecting a coarser-than-base grain or a missing dimension;
4. folds each metric's own filter (and the requested filters) into the right
   place — measure-level filters become *filtered aggregate columns* keyed so two
   metrics over the same measure with different filters never collide;
5. lays out the **post-aggregation computations** in dependency order so a ratio
   sees its operands, a derived metric sees its inputs, and cumulative/comparison
   passes run last.

Everything is pure: same graph + same query -> byte-identical plan (and
fingerprint). No I/O, no clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.lakehouse.semantic.metrics import (
    CumulativeMetric,
    DerivedMetric,
    Metric,
    RatioMetric,
    SimpleMetric,
    TimeComparisonMetric,
    requires_time,
)
from app.lakehouse.semantic.model import Dimension, JoinType, SemanticModel
from app.lakehouse.semantic.plan import (
    AggExpr,
    AggregationPlan,
    CumulativeComputation,
    DerivedComputation,
    GroupKey,
    JoinStep,
    MeasureProjection,
    MetricComputation,
    QueryPlan,
    RatioComputation,
    TimeComparisonComputation,
)
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import MeasureRef, SemanticGraph
from app.lakehouse.semantic.types import (
    Comparison,
    FieldRef,
    FilterExpr,
    Predicate,
    TimeGrain,
    and_all,
    is_coarser_or_equal,
)


class CompileError(ValueError):
    """Raised when a query cannot be lowered against the semantic graph."""


def _filtered_measure_key(ref: MeasureRef, metric_filter: FilterExpr | None) -> str:
    """Stable aggregate-column key for a measure under an (optional) metric filter.

    Two metrics over the same measure but different filters must produce two
    distinct aggregate columns; an unfiltered use reuses the bare ``MeasureRef``
    key. The filter ``repr`` is stable for frozen dataclasses, so the key is
    deterministic.
    """
    if metric_filter is None:
        return ref.key
    return f"{ref.key}#{hash(repr(metric_filter)) & 0xFFFFFFFF:08x}"


class Compiler:
    """Lowers metric queries against a fixed semantic graph."""

    def __init__(self, graph: SemanticGraph):
        self.graph = graph

    def compile(self, query: MetricQuery) -> QueryPlan:
        for name in query.metrics:
            if not self.graph.has_metric(name):
                raise CompileError(f"unknown metric {name!r}")

        ordered_metrics = self.graph.dependency_order_for(query.metrics)
        base_model = self._choose_base_model(query, ordered_metrics)

        # Resolve grouping dimensions + the time bucket key.
        group_keys, time_output = self._build_group_keys(query, base_model)

        # Collect & dedupe base aggregate columns (with per-metric filter folding).
        aggregates, projections = self._build_aggregates(ordered_metrics)

        # Assemble the join chain covering every model an aggregate touches.
        joins = self._build_joins(base_model, aggregates)

        # Post-aggregation computations, in dependency order.
        computations = self._build_computations(
            ordered_metrics, query.metrics, projections, query.time_grain
        )

        row_filter = and_all(*query.filters)
        time_window_filter = self._time_window_filter(query, base_model)

        aggregation = AggregationPlan(
            base_model=base_model.name,
            base_source=base_model.source,
            joins=joins,
            group_keys=group_keys,
            aggregates=aggregates,
            row_filter=row_filter,
            time_window_filter=time_window_filter,
        )
        return QueryPlan(
            aggregation=aggregation,
            computations=computations,
            output_metrics=query.metrics,
            dimension_outputs=tuple(
                g.output for g in group_keys if not g.is_time
            ),
            time_output=time_output,
            time_grain=query.time_grain,
            order_by=query.order_by,
            limit=query.limit,
        )

    # -- base model selection --------------------------------------------- #

    def _choose_base_model(
        self, query: MetricQuery, ordered_metrics: Sequence[str]
    ) -> SemanticModel:
        """Pick the model the grouping/time live on (the ``FROM`` anchor).

        A grouped query must hang every dimension off one base model and reach the
        measure models via many-to-one joins. We prefer a model that *owns the
        group-by dimensions* (and the time dimension); if there are no
        dimensions, we anchor on the first requested metric's first base measure.
        """
        # Models touched by the requested metrics' base measures.
        touched: list[str] = []
        for name in query.metrics:
            for ref in self.graph.base_measures(name):
                if ref.model not in touched:
                    touched.append(ref.model)

        # If dimensions are present, the base model must own all of them.
        dim_refs = list(query.group_by)
        if query.time_dimension is not None:
            dim_refs.append(query.time_dimension)

        if dim_refs:
            candidates = self._models_owning(dim_refs)
            if not candidates:
                raise CompileError(
                    "no single model owns all requested dimensions "
                    f"{[r.qualified for r in dim_refs]}"
                )
            # Prefer a candidate that is also a measure model (fewer joins),
            # else any candidate (joins resolve the rest).
            for cand in candidates:
                if cand in touched:
                    return self.graph.model(cand)
            return self.graph.model(candidates[0])

        # No dimensions: anchor on the first metric's first base measure model.
        if not touched:
            raise CompileError("query selects no measure-bearing metrics")
        return self.graph.model(touched[0])

    def _models_owning(self, refs: Sequence[FieldRef]) -> list[str]:
        """Models that contain *every* dimension in ``refs`` (qualified-aware)."""
        out: list[str] = []
        for model in self.graph.models.values():
            if all(self._model_has_dim(model, r) for r in refs):
                out.append(model.name)
        return out

    @staticmethod
    def _model_has_dim(model: SemanticModel, ref: FieldRef) -> bool:
        if ref.entity is not None and ref.entity != model.name:
            return False
        return model.has_dimension(ref.name)

    # -- group keys + time ------------------------------------------------- #

    def _build_group_keys(
        self, query: MetricQuery, base_model: SemanticModel
    ) -> tuple[tuple[GroupKey, ...], str | None]:
        keys: list[GroupKey] = []
        time_output: str | None = None

        if query.time_grain is not None:
            time_dim = self._resolve_time_dimension(query, base_model)
            if not is_coarser_or_equal(query.time_grain, time_dim.base_grain):  # type: ignore[arg-type]
                raise CompileError(
                    f"time grain {query.time_grain} is finer than {time_dim.name!r}'s "
                    f"base grain {time_dim.base_grain}"
                )
            time_output = time_dim.name
            keys.append(
                GroupKey(
                    output=time_dim.name,
                    model=base_model.name,
                    expr=time_dim.expression,
                    grain=query.time_grain,
                    is_time=True,
                )
            )

        for ref in query.group_by:
            if not self._model_has_dim(base_model, ref):
                raise CompileError(
                    f"dimension {ref.qualified!r} is not on base model {base_model.name!r}"
                )
            dim = base_model.dimension(ref.name)
            keys.append(
                GroupKey(
                    output=dim.name,
                    model=base_model.name,
                    expr=dim.expression,
                    grain=None,
                    is_time=False,
                )
            )
        return tuple(keys), time_output

    def _resolve_time_dimension(
        self, query: MetricQuery, base_model: SemanticModel
    ) -> Dimension:
        if query.time_dimension is not None:
            ref = query.time_dimension
            if not self._model_has_dim(base_model, ref):
                raise CompileError(
                    f"time dimension {ref.qualified!r} is not on base model "
                    f"{base_model.name!r}"
                )
            dim = base_model.dimension(ref.name)
            if not dim.is_time:
                raise CompileError(f"dimension {ref.qualified!r} is not a time dimension")
            return dim
        times = base_model.time_dimensions()
        if not times:
            raise CompileError(
                f"base model {base_model.name!r} has no time dimension for a time grain"
            )
        return times[0]

    def _time_window_filter(
        self, query: MetricQuery, base_model: SemanticModel
    ) -> FilterExpr | None:
        if query.time_window is None:
            return None
        window = query.time_window
        # Resolve against the same time dimension the grain buckets on (explicit
        # or the base model's default); MetricQuery guarantees a grain is set.
        time_dim = self._resolve_time_dimension(query, base_model)
        field = FieldRef(name=time_dim.name, entity=base_model.name)
        return and_all(
            Predicate(field=field, op=Comparison.GTE, value=window.start),
            Predicate(field=field, op=Comparison.LT, value=window.end),
        )

    # -- aggregate columns ------------------------------------------------- #

    def _build_aggregates(
        self, ordered_metrics: Sequence[str]
    ) -> tuple[tuple[AggExpr, ...], dict[str, str]]:
        """Build the deduped aggregate columns + a map ``metric -> agg column``.

        ``projections`` maps each *simple* metric to the aggregate column its
        per-bucket value comes from. Composite metrics read those columns via the
        post-aggregation computations.
        """
        agg_by_key: dict[str, AggExpr] = {}
        projections: dict[str, str] = {}
        for name in ordered_metrics:
            metric = self.graph.metric(name)
            if isinstance(metric, SimpleMetric):
                ref = self.graph.resolve_measure(metric.measure, metric.model)
                folded = self._fold_measure_filter(ref, metric.metric_filter)
                col = _filtered_measure_key(ref, folded)
                if col not in agg_by_key:
                    agg_by_key[col] = AggExpr(
                        output=col,
                        model=ref.model,
                        expr=ref.measure.expression,
                        agg=ref.measure.agg,
                        measure_filter=folded,
                    )
                projections[name] = col
        return tuple(agg_by_key.values()), projections

    @staticmethod
    def _fold_measure_filter(
        ref: MeasureRef, metric_filter: FilterExpr | None
    ) -> FilterExpr | None:
        """Conjoin a measure's intrinsic filter with the metric's own filter."""
        return and_all(ref.measure.measure_filter, metric_filter)

    # -- joins ------------------------------------------------------------- #

    def _build_joins(
        self, base_model: SemanticModel, aggregates: Sequence[AggExpr]
    ) -> tuple[JoinStep, ...]:
        needed: list[str] = []
        for agg in aggregates:
            if agg.model != base_model.name and agg.model not in needed:
                needed.append(agg.model)
        steps: list[JoinStep] = []
        seen_edges: set[tuple[str, str]] = set()
        for target in needed:
            path = self.graph.join_path(base_model.name, target)
            for left, right in zip(path, path[1:], strict=False):
                edge = (left, right)
                if edge in seen_edges:
                    continue
                join = self.graph.model(left).join_to(right)
                if join is None:  # pragma: no cover - join_path guarantees this
                    raise CompileError(f"missing declared join {left!r} -> {right!r}")
                if not join.many_to_one:
                    raise CompileError(
                        f"join {left!r} -> {right!r} is not many-to-one; aggregating "
                        "across it would fan out rows"
                    )
                seen_edges.add(edge)
                steps.append(
                    JoinStep(
                        left_model=left,
                        right_model=right,
                        left_key=join.from_key,
                        right_key=join.to_key,
                        join_type=join.join_type or JoinType.LEFT,
                    )
                )
        return tuple(steps)

    # -- post-aggregation computations ------------------------------------- #

    def _build_computations(
        self,
        ordered_metrics: Sequence[str],
        requested: Sequence[str],
        projections: dict[str, str],
        time_grain: TimeGrain | None,
    ) -> tuple[MetricComputation, ...]:
        comps: list[MetricComputation] = []
        for name in ordered_metrics:
            metric = self.graph.metric(name)
            if requires_time(metric) and time_grain is None:
                raise CompileError(
                    f"metric {name!r} is time-dependent and requires a time_grain"
                )
            comps.append(self._computation_for(name, metric, projections))
        return tuple(comps)

    def _computation_for(
        self, name: str, metric: Metric, projections: dict[str, str]
    ) -> MetricComputation:
        if isinstance(metric, SimpleMetric):
            return MeasureProjection(metric=name, source_column=projections[name])
        if isinstance(metric, RatioMetric):
            return RatioComputation(
                metric=name,
                numerator=metric.numerator,
                denominator=metric.denominator,
            )
        if isinstance(metric, DerivedMetric):
            return DerivedComputation(
                metric=name,
                expr=metric.expr,
                inputs=tuple((alias, target) for alias, target in metric.inputs.items()),
            )
        if isinstance(metric, CumulativeMetric):
            return CumulativeComputation(
                metric=name,
                base=metric.base,
                window=metric.window,
                periods=metric.periods,
            )
        if isinstance(metric, TimeComparisonMetric):
            return TimeComparisonComputation(
                metric=name,
                base=metric.base,
                offset_periods=metric.offset_periods,
                calculation=metric.calculation,
            )
        raise CompileError(f"unsupported metric kind for {name!r}")  # pragma: no cover


def compile_query(graph: SemanticGraph, query: MetricQuery) -> QueryPlan:
    """Convenience: compile ``query`` against ``graph`` (the functional entrypoint)."""
    return Compiler(graph).compile(query)


__all__ = ["CompileError", "Compiler", "compile_query"]
