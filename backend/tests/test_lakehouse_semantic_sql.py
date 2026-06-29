"""SQL-fallback renderer tests — deterministic text + parameter checks.

The renderer is pure (text + bound params), so we pin the emitted SQL shape and,
critically, that *every literal is parameterised* (injection-safety) while
identifiers are quoted bare names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.lakehouse.semantic.compiler import compile_query
from app.lakehouse.semantic.kpis import buffer_kpi_metrics, kpi_metrics
from app.lakehouse.semantic.query import MetricQuery, TimeWindow
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.sql import render_sql
from app.lakehouse.semantic.types import (
    Comparison,
    FieldRef,
    Predicate,
    TimeGrain,
)
from tests.lakehouse_fixtures import books_model, buffer_model, shots_model


def _graph() -> SemanticGraph:
    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


def _sql(query: MetricQuery) -> tuple[str, dict[str, Any]]:
    plan = compile_query(_graph(), query)
    rendered = render_sql(plan.aggregation)
    return rendered.sql, rendered.params


def test_simple_aggregate_sql() -> None:
    sql, params = _sql(MetricQuery.of("total_video_seconds"))
    assert 'sum("shots"."seconds")' in sql
    assert 'from "fact_shots" as "shots"' in sql
    assert params == {}


def test_group_by_emits_group_clause() -> None:
    sql, _ = _sql(MetricQuery.of("shot_total", group_by=("agent_role",)))
    assert 'group by "shots"."agent_role"' in sql
    assert 'as "agent_role"' in sql


def test_measure_filter_becomes_filter_aggregate() -> None:
    # rejected_seconds folds accepted=False -> a FILTER (WHERE ...) and a param.
    sql, params = _sql(MetricQuery.of("rejected_video_seconds"))
    assert "filter (where" in sql
    assert False in params.values()  # the accepted=False literal is bound


def test_time_grain_emits_date_trunc() -> None:
    sql, _ = _sql(
        MetricQuery.of(
            "total_video_seconds",
            time_grain=TimeGrain.DAY,
            time_dimension="rendered_at",
        )
    )
    assert "date_trunc('day', \"shots\".\"rendered_at\")" in sql


def test_row_filter_is_parameterised_not_inlined() -> None:
    sql, params = _sql(
        MetricQuery(
            metrics=("shot_total",),
            filters=(
                Predicate(
                    field=FieldRef(name="agent_role"),
                    op=Comparison.EQ,
                    value="generator'); drop table x;--",
                ),
            ),
        )
    )
    # The malicious value must be a bound parameter, never in the SQL text.
    assert "drop table" not in sql.lower()
    assert "generator'); drop table x;--" in params.values()
    assert ":p0" in sql


def test_in_predicate_expands_placeholders() -> None:
    sql, params = _sql(
        MetricQuery(
            metrics=("shot_total",),
            filters=(
                Predicate(
                    field=FieldRef(name="agent_role"),
                    op=Comparison.IN,
                    value=("generator", "showrunner"),
                ),
            ),
        )
    )
    assert 'IN (:p0, :p1)' in sql
    assert set(params.values()) == {"generator", "showrunner"}


def test_time_window_renders_two_bound_bounds() -> None:
    sql, params = _sql(
        MetricQuery.of(
            "shot_total",
            time_grain=TimeGrain.DAY,
            time_dimension="rendered_at",
            time_window=TimeWindow(
                start=datetime(2026, 6, 1, tzinfo=UTC),
                end=datetime(2026, 6, 3, tzinfo=UTC),
            ),
        )
    )
    assert sql.count(":p") == 2
    assert len(params) == 2


def test_sum_boolean_renders_count_filter() -> None:
    # accepted_shot_count uses SUM_BOOLEAN over `accepted`.
    from app.lakehouse.semantic.metrics import SimpleMetric

    graph = SemanticGraph.build(
        [shots_model(), books_model()],
        [SimpleMetric(name="accepted_shots", measure="accepted_shot_count")],
    )
    plan = compile_query(graph, MetricQuery.of("accepted_shots"))
    sql = render_sql(plan.aggregation).sql
    assert "count(*) filter (where" in sql
