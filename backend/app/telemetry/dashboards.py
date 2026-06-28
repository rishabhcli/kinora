"""Dashboards-as-code — Grafana dashboard JSON models for the Kinora signals.

Two dashboards are generated from code (so they version with the metrics rather
than being clicked together in a UI):

* **overview** — the RED view of the API + the USE view of the workers + the
  budget/buffer headline gauges (the operator dashboard);
* **crew** — the §13 per-agent quality/cost panels + the conflict/QA timeline
  (the demo "metrics panel" backing data).

The output is the Grafana dashboard JSON model (importable as-is). Panels are
built by a couple of small factories so the dashboards stay terse and consistent.
No Grafana dependency — this just emits the documented JSON shape.
"""

from __future__ import annotations

import itertools
from typing import Any

_DATASOURCE = "${DS_PROMETHEUS}"
_id_counter = itertools.count(1)


def _next_id() -> int:
    return next(_id_counter)


def _grid(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def _target(expr: str, legend: str = "") -> dict[str, Any]:
    return {"expr": expr, "legendFormat": legend, "refId": "A", "datasource": _DATASOURCE}


def _timeseries(
    title: str, expr: str, grid_pos: dict[str, int], *, unit: str = "short", legend: str = ""
) -> dict[str, Any]:
    return {
        "id": _next_id(),
        "type": "timeseries",
        "title": title,
        "datasource": _DATASOURCE,
        "gridPos": grid_pos,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [_target(expr, legend)],
    }


def _stat(
    title: str, expr: str, grid_pos: dict[str, int], *, unit: str = "short"
) -> dict[str, Any]:
    return {
        "id": _next_id(),
        "type": "stat",
        "title": title,
        "datasource": _DATASOURCE,
        "gridPos": grid_pos,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [_target(expr)],
    }


def _dashboard(
    title: str, uid: str, panels: list[dict[str, Any]], tags: list[str]
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["kinora", *tags],
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "10s",
        "templating": {
            "list": [
                {
                    "name": "DS_PROMETHEUS",
                    "type": "datasource",
                    "query": "prometheus",
                    "label": "Prometheus",
                }
            ]
        },
        "panels": panels,
    }


def overview_dashboard() -> dict[str, Any]:
    """The RED (API) + USE (workers) + budget/buffer operator dashboard."""
    panels = [
        _timeseries(
            "API request rate (RED: Rate)",
            "sum by (path) (rate(kinora_http_requests_total[5m]))",
            _grid(0, 0, 12, 8),
            unit="reqps",
            legend="{{path}}",
        ),
        _timeseries(
            "API error ratio (RED: Errors)",
            'sum(rate(kinora_http_requests_total{status=~"5.."}[5m])) '
            "/ sum(rate(kinora_http_requests_total[5m]))",
            _grid(12, 0, 12, 8),
            unit="percentunit",
            legend="5xx ratio",
        ),
        _timeseries(
            "API latency p95 (RED: Duration)",
            "histogram_quantile(0.95, sum by (le) "
            "(rate(kinora_http_request_duration_seconds_bucket[5m])))",
            _grid(0, 8, 12, 8),
            unit="s",
            legend="p95",
        ),
        _timeseries(
            "Queue depth per lane (USE: Saturation)",
            "kinora_queue_depth",
            _grid(12, 8, 12, 8),
            legend="{{lane}}",
        ),
        _timeseries(
            "Worker utilization (USE: Utilization)",
            "kinora_worker_busy_ratio",
            _grid(0, 16, 12, 8),
            unit="percentunit",
            legend="{{worker}}",
        ),
        _timeseries(
            "Dead-letters + retries (USE: Errors)",
            "rate(kinora_dlq_total[15m])",
            _grid(12, 16, 12, 8),
            legend="dlq",
        ),
        _stat(
            "Video-seconds spent (budget burn)",
            "kinora_video_seconds_spent_total",
            _grid(0, 24, 8, 6),
            unit="s",
        ),
        _timeseries(
            "Buffer occupancy (the §4.10 sawtooth)",
            "kinora_buffer_occupancy_seconds",
            _grid(8, 24, 16, 6),
            unit="s",
            legend="{{session}}",
        ),
    ]
    return _dashboard("Kinora — Overview (RED/USE)", "kinora-overview", panels, ["red", "use"])


def crew_dashboard() -> dict[str, Any]:
    """The §13 per-agent quality/cost demo panel."""
    panels = [
        _timeseries(
            "Per-agent calls",
            "kinora_agent_calls_total_gauge",
            _grid(0, 0, 12, 8),
            legend="{{agent}}",
        ),
        _timeseries(
            "Per-agent cost (USD)",
            "kinora_agent_cost_usd_gauge",
            _grid(12, 0, 12, 8),
            unit="currencyUSD",
            legend="{{agent}}",
        ),
        _timeseries(
            "Per-agent p95 latency",
            "kinora_agent_latency_p95_seconds",
            _grid(0, 8, 12, 8),
            unit="s",
            legend="{{agent}}",
        ),
        _timeseries(
            "Per-agent JSON-repair count",
            "kinora_agent_repairs_gauge",
            _grid(12, 8, 12, 8),
            legend="{{agent}}",
        ),
        _timeseries(
            "Mean CCS attributed (target ≥ 0.85)",
            "kinora_agent_mean_ccs_gauge",
            _grid(0, 16, 12, 8),
            unit="percentunit",
            legend="{{agent}}",
        ),
        _timeseries(
            "QA pass-rate — accepted vs degraded",
            "sum(rate(kinora_shots_accepted_total[15m])) "
            "/ (sum(rate(kinora_shots_accepted_total[15m])) "
            "+ sum(rate(kinora_shots_degraded_total[15m])))",
            _grid(12, 16, 12, 8),
            unit="percentunit",
            legend="acceptance",
        ),
        _timeseries(
            "Conflicts resolved by policy (§7.2)",
            "rate(kinora_conflicts_resolved_total[15m])",
            _grid(0, 24, 24, 7),
            legend="{{option}}",
        ),
    ]
    return _dashboard("Kinora — Crew (§13 quality/cost)", "kinora-crew", panels, ["crew", "eval"])


#: The dashboards keyed by name (for the read endpoint / file dump).
_DASHBOARDS = {
    "overview": overview_dashboard,
    "crew": crew_dashboard,
}


def dashboard_names() -> list[str]:
    """Return the available dashboard names."""
    return list(_DASHBOARDS)


def build_dashboard(name: str) -> dict[str, Any] | None:
    """Build one dashboard JSON model by name (``None`` if unknown).

    Each call rebuilds with a fresh panel-id sequence so ids are stable within a
    single dashboard.
    """
    factory = _DASHBOARDS.get(name)
    if factory is None:
        return None
    global _id_counter
    _id_counter = itertools.count(1)
    return factory()


def all_dashboards() -> dict[str, dict[str, Any]]:
    """Build every dashboard, keyed by name."""
    return {name: build_dashboard(name) for name in _DASHBOARDS}  # type: ignore[misc]


__all__ = [
    "all_dashboards",
    "build_dashboard",
    "crew_dashboard",
    "dashboard_names",
    "overview_dashboard",
]
