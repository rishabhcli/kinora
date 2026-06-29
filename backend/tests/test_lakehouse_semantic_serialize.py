"""Serialisation DTO tests — JSON-safety + chart/table/explain shapes."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.lakehouse.semantic.catalog import MetricsCatalog
from app.lakehouse.semantic.compiler import compile_query
from app.lakehouse.semantic.executor import execute_plan
from app.lakehouse.semantic.kpis import KPI_CATALOG_TAGS, buffer_kpi_metrics, kpi_metrics
from app.lakehouse.semantic.lineage import LineageGraph
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.serialize import (
    catalog_to_dict,
    jsonify,
    lineage_to_dict,
    plan_to_dict,
    result_to_dict,
    result_to_series,
)
from app.lakehouse.semantic.types import TimeGrain
from tests.lakehouse_fixtures import books_model, buffer_model, make_engine, shots_model


def _graph() -> SemanticGraph:
    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


def test_jsonify_handles_datetimes_and_enums() -> None:
    out = jsonify({"t": datetime(2026, 6, 1, tzinfo=UTC), "g": TimeGrain.DAY, "xs": (1, 2)})
    assert out == {"t": "2026-06-01T00:00:00+00:00", "g": "day", "xs": [1, 2]}
    # And the whole thing is genuinely json-serialisable.
    json.dumps(out)


def test_result_to_dict_is_json_safe() -> None:
    plan = compile_query(
        _graph(),
        MetricQuery.of(
            "usd_total",
            "budget_burn",
            time_grain=TimeGrain.DAY,
            time_dimension="rendered_at",
        ),
    )
    result = execute_plan(plan, make_engine())
    dto = result_to_dict(result)
    json.dumps(dto)  # must not raise
    assert dto["time_column"] == "rendered_at"
    assert "budget_burn" in dto["metrics"]
    # The time bucket value was ISO-stringified.
    assert isinstance(dto["rows"][0]["rendered_at"], str)


def test_result_to_series_chart_shape() -> None:
    plan = compile_query(
        _graph(),
        MetricQuery.of("shot_total", group_by=("agent_role",)),
    )
    result = execute_plan(plan, make_engine())
    series = result_to_series(result)
    assert series["x_axis"] == "agent_role"
    shot_series = next(s for s in series["series"] if s["metric"] == "shot_total")
    assert {p["x"] for p in shot_series["points"]} == {"generator", "showrunner"}


def test_plan_to_dict_explain() -> None:
    plan = compile_query(_graph(), MetricQuery.of("accepted_footage_efficiency"))
    dto = plan_to_dict(plan)
    json.dumps(dto)
    assert dto["base_model"] == "shots"
    assert dto["fingerprint"] == plan.fingerprint()
    assert "accepted_footage_efficiency" in dto["output_metrics"]


def test_catalog_to_dict() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    dto = catalog_to_dict(cat)
    json.dumps(dto)
    names = {m["name"] for m in dto["metrics"]}
    assert "accepted_footage_efficiency" in names
    assert "headline" in dto["groups"]
    assert "ratio" in dto["kinds"]


def test_lineage_to_dict() -> None:
    lg = LineageGraph(_graph())
    dto = lineage_to_dict(lg.lineage_of("accepted_footage_efficiency"))
    json.dumps(dto)
    assert dto["metric"] == "accepted_footage_efficiency"
    assert dto["models"] == ["shots"]
