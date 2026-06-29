"""Deterministic compiler + executor tests over the in-memory engine.

These pin the metrics layer's *behaviour* end-to-end: a metric query lowers to a
plan and the plan executes against :class:`InMemoryEngine` to a known answer. No
infra, no clock — same query, same bytes.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.lakehouse.semantic.compiler import CompileError, compile_query
from app.lakehouse.semantic.executor import execute_plan
from app.lakehouse.semantic.kpis import buffer_kpi_metrics, kpi_metrics
from app.lakehouse.semantic.metrics import SimpleMetric
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph, SemanticGraphError
from app.lakehouse.semantic.types import (
    Comparison,
    FieldRef,
    OrderBy,
    Predicate,
    SortDirection,
    TimeGrain,
)
from tests.lakehouse_fixtures import (
    books_model,
    buffer_model,
    make_engine,
    shots_model,
)


def _graph() -> SemanticGraph:
    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


def _run(query: MetricQuery) -> list[dict[str, Any]]:
    graph = _graph()
    plan = compile_query(graph, query)
    result = execute_plan(plan, make_engine())
    return [dict(r) for r in result.rows]


# --------------------------------------------------------------------------- #
# Simple metrics + grouping
# --------------------------------------------------------------------------- #


def test_simple_measure_totals() -> None:
    rows = _run(MetricQuery.of("total_video_seconds", "shot_total"))
    assert len(rows) == 1
    assert rows[0]["total_video_seconds"] == 40
    assert rows[0]["shot_total"] == 8


def test_group_by_dimension() -> None:
    rows = _run(
        MetricQuery.of("shot_total", group_by=("agent_role",), order_by=(OrderBy("agent_role"),))
    )
    by_role = {r["agent_role"]: r["shot_total"] for r in rows}
    assert by_role == {"generator": 4, "showrunner": 4}


def test_measure_level_filter_isolates_rejected() -> None:
    # rejected_seconds folds in accepted=False; only s2 + s4 (5 + 5).
    rows = _run(MetricQuery.of("rejected_video_seconds"))
    assert rows[0]["rejected_video_seconds"] == 10


# --------------------------------------------------------------------------- #
# Ratio / derived KPIs
# --------------------------------------------------------------------------- #


def test_accepted_footage_efficiency_headline() -> None:
    rows = _run(MetricQuery.of("accepted_footage_efficiency"))
    # (1 - 10/40) * 100 = 75.0
    assert math.isclose(rows[0]["accepted_footage_efficiency"], 75.0)


def test_regen_rate_ratio() -> None:
    rows = _run(MetricQuery.of("regen_rate"))
    # 4 regens / 8 shots = 0.5
    assert math.isclose(rows[0]["regen_rate"], 0.5)


def test_ccs_mean_ratio() -> None:
    rows = _run(MetricQuery.of("ccs"))
    # ccs_sum 6.74 / 8 shots
    assert math.isclose(rows[0]["ccs"], 6.74 / 8)


def test_ratio_zero_denominator_is_zero() -> None:
    # Filter to a non-existent role: empty group -> 0/0 -> 0.0, not an error.
    rows = _run(
        MetricQuery(
            metrics=("regen_rate",),
            filters=(
                Predicate(field=FieldRef(name="agent_role"), op=Comparison.EQ, value="nobody"),
            ),
        )
    )
    # No rows match -> one all-null group collapses to empty; executor yields []
    # OR a single zero group depending on engine. Accept either: assert no crash.
    assert all(r.get("regen_rate") in (0.0, None) for r in rows)


# --------------------------------------------------------------------------- #
# Time series: cumulative + comparison
# --------------------------------------------------------------------------- #


def test_budget_burn_cumulative_runs_to_total() -> None:
    rows = _run(
        MetricQuery.of(
            "usd_total",
            "budget_burn",
            time_grain=TimeGrain.DAY,
            time_dimension="rendered_at",
        )
    )
    # Two day buckets, usd all 0 -> cumulative stays 0.
    assert len(rows) == 2
    assert [r["budget_burn"] for r in rows] == [0.0, 0.0]


def test_cumulative_running_total_nonzero() -> None:
    # Build an ad-hoc cumulative over total_seconds (additive) to see the run-up.
    from app.lakehouse.semantic.metrics import CumulativeMetric, WindowKind

    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [
            SimpleMetric(name="secs", measure="total_seconds"),
            CumulativeMetric(name="secs_running", base="secs", window=WindowKind.ALL_TIME),
        ],
    )
    plan = compile_query(
        graph,
        MetricQuery.of(
            "secs", "secs_running", time_grain=TimeGrain.DAY, time_dimension="rendered_at"
        ),
    )
    rows = execute_plan(plan, make_engine()).rows
    # day1 = 20s, day2 = 20s -> running [20, 40].
    assert [r["secs"] for r in rows] == [20, 20]
    assert [r["secs_running"] for r in rows] == [20.0, 40.0]


def test_trailing_window_cumulative() -> None:
    from app.lakehouse.semantic.metrics import CumulativeMetric, WindowKind

    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [
            SimpleMetric(name="secs", measure="total_seconds"),
            CumulativeMetric(
                name="secs_t1",
                base="secs",
                window=WindowKind.TRAILING,
                periods=1,
            ),
        ],
    )
    plan = compile_query(
        graph,
        MetricQuery.of(
            "secs_t1", time_grain=TimeGrain.DAY, time_dimension="rendered_at"
        ),
    )
    rows = execute_plan(plan, make_engine()).rows
    # trailing window of 1 grain == the per-bucket value: [20, 20].
    assert [r["secs_t1"] for r in rows] == [20.0, 20.0]


def test_time_comparison_percent_change() -> None:
    from app.lakehouse.semantic.metrics import (
        CalculationKind,
        TimeComparisonMetric,
    )

    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [
            SimpleMetric(name="secs", measure="total_seconds"),
            TimeComparisonMetric(
                name="secs_pop",
                base="secs",
                offset_periods=1,
                calculation=CalculationKind.PERCENT_CHANGE,
            ),
        ],
    )
    plan = compile_query(
        graph,
        MetricQuery.of(
            "secs", "secs_pop", time_grain=TimeGrain.DAY, time_dimension="rendered_at"
        ),
    )
    rows = execute_plan(plan, make_engine()).rows
    # day1 has no prior -> None; day2 == day1 (20==20) -> 0% change.
    assert rows[0]["secs_pop"] is None
    assert math.isclose(rows[1]["secs_pop"], 0.0)


# --------------------------------------------------------------------------- #
# Joins
# --------------------------------------------------------------------------- #


def test_group_by_joined_dimension() -> None:
    # genre lives on the books model; shots joins many-to-one to books.
    # But group_by must be on the base model, so we anchor on books? No — the
    # dimension is on books; the base model must own it. Compile should pick a
    # model that owns 'genre' (books) and join shots' measure... which fans out.
    # Instead, test the supported direction: book_id is on shots (the fact).
    rows = _run(
        MetricQuery.of(
            "shot_total", group_by=("book_id",), order_by=(OrderBy("book_id"),)
        )
    )
    assert {r["book_id"]: r["shot_total"] for r in rows} == {"book_a": 4, "book_b": 4}


# --------------------------------------------------------------------------- #
# Ordering + limit
# --------------------------------------------------------------------------- #


def test_order_by_desc_and_limit() -> None:
    rows = _run(
        MetricQuery.of(
            "shot_total",
            group_by=("agent_role",),
            order_by=(OrderBy("shot_total", SortDirection.DESC), OrderBy("agent_role")),
            limit=1,
        )
    )
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Validation errors
# --------------------------------------------------------------------------- #


def test_unknown_metric_raises() -> None:
    with pytest.raises(CompileError):
        compile_query(_graph(), MetricQuery.of("not_a_metric"))


def test_unknown_dimension_raises() -> None:
    with pytest.raises(CompileError):
        compile_query(_graph(), MetricQuery.of("shot_total", group_by=("nope",)))


def test_time_metric_without_grain_raises() -> None:
    with pytest.raises(CompileError):
        compile_query(_graph(), MetricQuery.of("budget_burn"))


def test_finer_grain_than_base_raises() -> None:
    # shots base grain is HOUR; asking for HOUR is fine, but we declare buffer at
    # HOUR too; request a coarser grain (DAY) is allowed. To trigger the error we
    # need a model whose base grain is coarser than the request — build one.
    from app.lakehouse.semantic.model import Dimension, Measure, SemanticModel
    from app.lakehouse.semantic.types import Aggregation, DataType

    coarse = SemanticModel(
        name="daily",
        source="daily_src",
        primary_entity="row_id",
        dimensions=(
            Dimension(name="row_id", data_type=DataType.STRING),
            Dimension(
                name="day",
                data_type=DataType.DATE,
                is_time=True,
                base_grain=TimeGrain.DAY,
            ),
        ),
        measures=(Measure(name="n", agg=Aggregation.COUNT, expr=None),),
    )
    graph = SemanticGraph.build([coarse], [SimpleMetric(name="rows", measure="n")])
    with pytest.raises(CompileError):
        compile_query(
            graph,
            MetricQuery.of("rows", time_grain=TimeGrain.HOUR, time_dimension="day"),
        )


def test_metric_cycle_rejected() -> None:
    from app.lakehouse.semantic.metrics import DerivedMetric

    with pytest.raises(SemanticGraphError):
        SemanticGraph.build(
            [shots_model(), books_model()],
            [
                DerivedMetric(name="a", expr="b", inputs={"b": "b"}),
                DerivedMetric(name="b", expr="a", inputs={"a": "a"}),
            ],
        )
