"""Plan-determinism + compiler-depth tests.

The compiler is the deterministic heart of the layer: same graph + same query ->
byte-identical plan and fingerprint. These tests pin that property and the
trickier compiler behaviours (filtered-measure deduplication, fan-out-safe
joins, multi-kind queries).
"""

from __future__ import annotations

import math

from app.lakehouse.semantic.compiler import CompileError, compile_query
from app.lakehouse.semantic.executor import execute_plan
from app.lakehouse.semantic.metrics import SimpleMetric
from app.lakehouse.semantic.model import (
    Dimension,
    Join,
    Measure,
    SemanticModel,
)
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.types import (
    Aggregation,
    Comparison,
    DataType,
    FieldRef,
    Predicate,
)
from tests.lakehouse_fixtures import (
    books_model,
    buffer_model,
    make_engine,
    shots_model,
)


def _graph() -> SemanticGraph:
    from app.lakehouse.semantic.kpis import buffer_kpi_metrics, kpi_metrics

    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_fingerprint_is_stable_across_compiles() -> None:
    graph = _graph()
    q = MetricQuery.of("accepted_footage_efficiency", group_by=("book_id",))
    fp1 = compile_query(graph, q).fingerprint()
    fp2 = compile_query(graph, q).fingerprint()
    assert fp1 == fp2


def test_fingerprint_differs_on_different_filters() -> None:
    graph = _graph()
    q1 = MetricQuery(metrics=("shot_total",))
    q2 = MetricQuery(
        metrics=("shot_total",),
        filters=(
            Predicate(field=FieldRef(name="agent_role"), op=Comparison.EQ, value="generator"),
        ),
    )
    assert compile_query(graph, q1).fingerprint() != compile_query(graph, q2).fingerprint()


def test_fingerprint_differs_on_grain() -> None:
    from app.lakehouse.semantic.types import TimeGrain

    graph = _graph()
    day = MetricQuery.of(
        "usd_total", "budget_burn", time_grain=TimeGrain.DAY, time_dimension="rendered_at"
    )
    month = MetricQuery.of(
        "usd_total", "budget_burn", time_grain=TimeGrain.MONTH, time_dimension="rendered_at"
    )
    assert compile_query(graph, day).fingerprint() != compile_query(graph, month).fingerprint()


# --------------------------------------------------------------------------- #
# Filtered-measure deduplication
# --------------------------------------------------------------------------- #


def test_same_measure_different_filters_distinct_columns() -> None:
    # Two simple metrics over the same `total_seconds` measure but different
    # metric filters must compile to TWO aggregate columns, not collide.
    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [
            SimpleMetric(
                name="gen_secs",
                measure="total_seconds",
                metric_filter=Predicate(
                    field=FieldRef(name="agent_role"), op=Comparison.EQ, value="generator"
                ),
            ),
            SimpleMetric(
                name="show_secs",
                measure="total_seconds",
                metric_filter=Predicate(
                    field=FieldRef(name="agent_role"), op=Comparison.EQ, value="showrunner"
                ),
            ),
        ],
    )
    plan = compile_query(graph, MetricQuery.of("gen_secs", "show_secs"))
    # Two distinct aggregate columns.
    assert len(plan.aggregation.aggregates) == 2
    result = execute_plan(plan, make_engine())
    # generator shots: s3,s4,s7,s8 = 20s; showrunner: s1,s2,s5,s6 = 20s.
    assert result.rows[0]["gen_secs"] == 20
    assert result.rows[0]["show_secs"] == 20


def test_identical_measure_same_filter_dedupes_to_one_column() -> None:
    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [
            SimpleMetric(name="a", measure="total_seconds"),
            SimpleMetric(name="b", measure="total_seconds"),
        ],
    )
    plan = compile_query(graph, MetricQuery.of("a", "b"))
    # Same measure, no filter -> ONE shared aggregate column.
    assert len(plan.aggregation.aggregates) == 1


# --------------------------------------------------------------------------- #
# Join fan-out safety
# --------------------------------------------------------------------------- #


def test_one_to_many_join_rejected() -> None:
    # A one-to-many join (many_to_one=False) would fan out rows; aggregating a
    # measure on the far side must be rejected.
    parent = SemanticModel(
        name="parent",
        source="parent_src",
        primary_entity="pid",
        dimensions=(Dimension(name="pid", data_type=DataType.STRING),),
        measures=(Measure(name="pcount", agg=Aggregation.COUNT, expr=None),),
        joins=(
            Join(
                to_model="child",
                from_key="pid",
                to_key="pid",
                many_to_one=False,  # one parent -> many children: fan-out
            ),
        ),
    )
    child = SemanticModel(
        name="child",
        source="child_src",
        primary_entity="cid",
        dimensions=(Dimension(name="cid", data_type=DataType.STRING),),
        measures=(Measure(name="ccount", agg=Aggregation.COUNT, expr=None),),
    )
    graph = SemanticGraph.build(
        [parent, child],
        [
            SimpleMetric(name="parents", measure="pcount"),
            SimpleMetric(name="children", measure="ccount"),
        ],
    )
    # Anchoring on parent and pulling a child measure crosses the fan-out edge.
    try:
        compile_query(graph, MetricQuery.of("parents", "children"))
        raise AssertionError("expected a fan-out CompileError")
    except CompileError as exc:
        assert "fan out" in str(exc)


# --------------------------------------------------------------------------- #
# Mixed-kind multi-metric query
# --------------------------------------------------------------------------- #


def test_mixed_kinds_in_one_query() -> None:
    graph = _graph()
    out = execute_plan(
        compile_query(
            graph,
            MetricQuery.of(
                "shot_total",  # simple
                "regen_rate",  # ratio
                "accepted_footage_efficiency",  # derived
            ),
        ),
        make_engine(),
    )
    row = out.rows[0]
    assert row["shot_total"] == 8
    assert math.isclose(row["regen_rate"], 0.5)
    assert math.isclose(row["accepted_footage_efficiency"], 75.0)


def test_intermediate_metrics_dropped_from_output() -> None:
    # efficiency depends on rejected/total simple metrics; they must NOT leak
    # into the output unless explicitly requested.
    graph = _graph()
    out = execute_plan(
        compile_query(graph, MetricQuery.of("accepted_footage_efficiency")),
        make_engine(),
    )
    assert set(out.rows[0]) == {"accepted_footage_efficiency"}
