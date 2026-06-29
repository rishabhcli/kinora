"""JSON-safe serialisation of results, plans, catalog, and lineage.

The self-serve query API returns over HTTP/SSE, so every public artefact needs a
deterministic, JSON-safe dict form. This module is the one place that knows how
to flatten the layer's frozen dataclasses into primitives (datetimes -> ISO-8601
UTC strings, enums -> their values, tuples -> lists) without dragging a
serialisation framework into the pure core.

Nothing here parses *in*; the loader (:mod:`app.lakehouse.semantic.loader`) owns
the inbound direction. These are strictly outbound DTO builders.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.lakehouse.semantic.catalog import MetricEntry, MetricsCatalog
from app.lakehouse.semantic.executor import MetricResult
from app.lakehouse.semantic.lineage import MetricLineage
from app.lakehouse.semantic.plan import QueryPlan


def jsonify(value: Any) -> Any:
    """Recursively coerce a value into a JSON-safe primitive tree."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonify(v) for v in value]
    # Enums (StrEnum) stringify cleanly; fall back to repr for anything exotic.
    return str(value)


def result_to_dict(result: MetricResult) -> dict[str, Any]:
    """A tabular DTO: ordered columns + JSON-safe row records.

    Shape::

        {"columns": [...], "dimensions": [...], "time_column": "...",
         "metrics": [...], "rows": [{col: value, ...}, ...]}
    """
    return {
        "columns": list(result.columns),
        "dimensions": list(result.dimensions),
        "time_column": result.time_column,
        "metrics": list(result.metrics),
        "rows": [{k: jsonify(v) for k, v in row.items()} for row in result.rows],
    }


def result_to_series(result: MetricResult) -> dict[str, Any]:
    """A chart-friendly DTO: one series of points per requested metric.

    Each series is ``{"metric": name, "points": [{"x": label, "y": value}, ...]}``
    where ``x`` is the time bucket (if a time series) else the joined dimension
    key. This is the shape the §13 metrics-panel sawtooth / KPI cards consume.
    """
    x_key = result.time_column or (result.dimensions[0] if result.dimensions else None)
    series: list[dict[str, Any]] = []
    for metric in result.metrics:
        points = [
            {"x": jsonify(row.get(x_key)) if x_key else None, "y": jsonify(row.get(metric))}
            for row in result.rows
        ]
        series.append({"metric": metric, "points": points})
    return {"x_axis": x_key, "series": series}


def plan_to_dict(plan: QueryPlan) -> dict[str, Any]:
    """A debug/inspection DTO for a compiled plan (the 'explain' payload)."""
    agg = plan.aggregation
    return {
        "fingerprint": plan.fingerprint(),
        "base_model": agg.base_model,
        "base_source": agg.base_source,
        "joins": [
            {
                "left": j.left_model,
                "right": j.right_model,
                "on": f"{j.left_key} = {j.right_key}",
                "type": j.join_type,
            }
            for j in agg.joins
        ],
        "group_keys": [
            {"output": g.output, "grain": jsonify(g.grain), "is_time": g.is_time}
            for g in agg.group_keys
        ],
        "aggregates": [
            {"output": a.output, "agg": jsonify(a.agg), "expr": a.expr} for a in agg.aggregates
        ],
        "output_metrics": list(plan.output_metrics),
        "time_grain": jsonify(plan.time_grain),
        "limit": plan.limit,
    }


def metric_entry_to_dict(entry: MetricEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "label": entry.label,
        "description": entry.description,
        "kind": jsonify(entry.kind),
        "format": entry.format,
        "tags": list(entry.tags),
        "time_dependent": entry.time_dependent,
        "base_measures": list(entry.base_measures),
        "upstream_metrics": list(entry.upstream_metrics),
        "models": list(entry.models),
    }


def catalog_to_dict(catalog: MetricsCatalog) -> dict[str, Any]:
    """The full catalog DTO: metrics + dimensions + facet indices."""
    return {
        "metrics": [metric_entry_to_dict(m) for m in catalog.metrics()],
        "dimensions": [
            {
                "name": d.name,
                "model": d.model,
                "label": d.label,
                "data_type": d.data_type,
                "is_time": d.is_time,
                "sensitive": d.sensitive,
            }
            for d in catalog.dimensions()
        ],
        "groups": {tag: list(names) for tag, names in catalog.groups().items()},
        "kinds": {kind: list(names) for kind, names in catalog.kinds().items()},
    }


def lineage_to_dict(lineage: MetricLineage) -> dict[str, Any]:
    return {
        "metric": lineage.metric,
        "upstream_metrics": list(lineage.upstream_metrics),
        "base_measures": list(lineage.base_measures),
        "physical_columns": [
            {"model": c.model, "source": c.source, "expression": c.expression}
            for c in lineage.physical_columns
        ],
        "models": list(lineage.models),
    }


__all__ = [
    "catalog_to_dict",
    "jsonify",
    "lineage_to_dict",
    "metric_entry_to_dict",
    "plan_to_dict",
    "result_to_dict",
    "result_to_series",
]
