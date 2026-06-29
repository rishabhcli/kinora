"""Declarative-loader tests — author the model + metrics as plain data.

Proves the dict spec round-trips into a working graph that compiles + executes
to the same numbers as the hand-built fixtures, and that malformed specs are
rejected at load time (typos surface early).
"""

from __future__ import annotations

import math

import pytest

from app.lakehouse.semantic.compiler import compile_query
from app.lakehouse.semantic.executor import execute_plan
from app.lakehouse.semantic.loader import (
    LoaderError,
    load_filter,
    load_graph,
)
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.types import (
    And,
    Comparison,
    Predicate,
)
from tests.lakehouse_fixtures import make_engine

SPEC = {
    "version": 1,
    "models": [
        {
            "name": "shots",
            "source": "fact_shots",
            "primary_entity": "shot_id",
            "dimensions": [
                {"name": "shot_id"},
                {"name": "book_id"},
                {"name": "agent_role", "label": "Agent Role"},
                {"name": "mode"},
                {
                    "name": "rendered_at",
                    "type": "timestamp",
                    "is_time": True,
                    "grain": "hour",
                },
            ],
            "measures": [
                {"name": "shot_count", "agg": "count"},
                {"name": "total_seconds", "agg": "sum", "expr": "seconds"},
                {
                    "name": "rejected_seconds",
                    "agg": "sum",
                    "expr": "seconds",
                    "filter": {"field": "accepted", "op": "eq", "value": False},
                },
                {"name": "regen_count", "agg": "sum", "expr": "regens"},
            ],
            "joins": [{"to": "books", "from_key": "book_id", "to_key": "book_id"}],
        },
        {
            "name": "books",
            "source": "dim_books",
            "primary_entity": "book_id",
            "dimensions": [{"name": "book_id"}, {"name": "genre"}],
            "measures": [{"name": "book_count", "agg": "count_distinct", "expr": "book_id"}],
        },
    ],
    "metrics": [
        {"name": "total_video_seconds", "kind": "simple", "measure": "total_seconds"},
        {"name": "rejected_video_seconds", "kind": "simple", "measure": "rejected_seconds"},
        {"name": "shot_total", "kind": "simple", "measure": "shot_count"},
        {"name": "regens_total", "kind": "simple", "measure": "regen_count"},
        {
            "name": "efficiency",
            "kind": "derived",
            "expr": "(1 - rejected / total) * 100",
            "inputs": {"rejected": "rejected_video_seconds", "total": "total_video_seconds"},
            "format": "percent",
        },
        {
            "name": "regen_rate",
            "kind": "ratio",
            "numerator": "regens_total",
            "denominator": "shot_total",
        },
        {
            "name": "secs_running",
            "kind": "cumulative",
            "base": "total_video_seconds",
            "window": "all_time",
        },
        {
            "name": "secs_pop",
            "kind": "time_comparison",
            "base": "total_video_seconds",
            "offset_periods": 1,
            "calculation": "delta",
        },
    ],
}


def test_loaded_graph_compiles_and_executes() -> None:
    graph = load_graph(SPEC)
    plan = compile_query(graph, MetricQuery.of("efficiency"))
    result = execute_plan(plan, make_engine())
    assert math.isclose(result.rows[0]["efficiency"], 75.0)


def test_loaded_ratio_and_cumulative() -> None:
    graph = load_graph(SPEC)
    eng = make_engine()
    rr = execute_plan(compile_query(graph, MetricQuery.of("regen_rate")), eng)
    assert math.isclose(rr.rows[0]["regen_rate"], 0.5)


def test_loaded_all_metric_kinds_present() -> None:
    graph = load_graph(SPEC)
    kinds = {m.kind.value for m in graph.metrics.values()}
    assert {"simple", "derived", "ratio", "cumulative", "time_comparison"} <= kinds


def test_filter_minilang_roundtrip() -> None:
    expr = load_filter(
        {
            "and": [
                {"field": "accepted", "op": "eq", "value": True},
                {"field": "agent_role", "op": "in", "value": ["a", "b"]},
            ]
        }
    )
    assert isinstance(expr, And)
    assert isinstance(expr.terms[0], Predicate)
    second = expr.terms[1]
    assert isinstance(second, Predicate)
    assert second.op is Comparison.IN
    assert second.value == ("a", "b")


def test_filter_none_is_none() -> None:
    assert load_filter(None) is None


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_unknown_key_rejected() -> None:
    with pytest.raises(LoaderError):
        load_graph(
            {
                "models": [
                    {
                        "name": "m",
                        "source": "s",
                        "primary_entity": "id",
                        "measures": [{"name": "n", "agg": "count", "typo_key": 1}],
                    }
                ]
            }
        )


def test_unknown_metric_kind_rejected() -> None:
    with pytest.raises(LoaderError):
        load_graph(
            {
                "models": [{"name": "m", "source": "s", "primary_entity": "id"}],
                "metrics": [{"name": "x", "kind": "nonsense"}],
            }
        )


def test_missing_required_key_rejected() -> None:
    with pytest.raises(LoaderError):
        load_graph({"models": [{"name": "m", "source": "s"}]})  # no primary_entity


def test_empty_models_rejected() -> None:
    with pytest.raises(LoaderError):
        load_graph({"models": []})


def test_in_filter_requires_list() -> None:
    with pytest.raises(LoaderError):
        load_filter({"field": "x", "op": "in", "value": "scalar"})
