"""Prometheus metrics for the durable-workflow engine (kinora.md §12.5).

Registers a small set of ``kinora_workflow_*`` series against the *shared*
registry from :mod:`app.observability.metrics`, so they show up on the existing
``/metrics`` endpoint without touching that module. Cardinality is bounded: the
per-workflow / per-activity label is the registered *type name* (a small, fixed
set), never a workflow/run id.

The worker loops call the tiny emit helpers here; metrics never leak Prometheus
types into the engine logic, and registration is guarded so importing this module
under a test reloader can't raise a duplicate-registration error.
"""

from __future__ import annotations

import contextlib

from prometheus_client import Counter, Gauge, Histogram

from app.observability.metrics import registry

with contextlib.suppress(ValueError):
    Counter(
        "kinora_workflow_executions_total",
        "Workflow executions reaching a terminal status, by type and status.",
        labelnames=("workflow_type", "status"),
        registry=registry,
    )
    Counter(
        "kinora_workflow_activity_runs_total",
        "Activity executions by type and outcome (succeeded/failed/timed_out/cancelled/retried).",
        labelnames=("activity_type", "outcome"),
        registry=registry,
    )
    Histogram(
        "kinora_workflow_task_duration_seconds",
        "Workflow-task replay+advance wall-clock duration, by workflow type.",
        labelnames=("workflow_type",),
        buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
        registry=registry,
    )
    Counter(
        "kinora_workflow_nondeterminism_total",
        "Non-determinism errors raised during replay, by workflow type.",
        labelnames=("workflow_type",),
        registry=registry,
    )
    Counter(
        "kinora_workflow_timers_fired_total",
        "Durable timers promoted to TIMER_FIRED.",
        registry=registry,
    )
    Gauge(
        "kinora_workflow_open_executions",
        "Current number of running (non-terminal) workflow executions.",
        registry=registry,
    )


def _metric(name: str):  # type: ignore[no-untyped-def]
    """Fetch a registered collector by name (None if metrics unavailable)."""
    return registry._names_to_collectors.get(name)  # noqa: SLF001


def record_execution_terminal(workflow_type: str, status: str) -> None:
    metric = _metric("kinora_workflow_executions_total")
    if metric is not None:
        metric.labels(workflow_type=workflow_type, status=status).inc()


def record_activity_outcome(activity_type: str, outcome: str) -> None:
    metric = _metric("kinora_workflow_activity_runs_total")
    if metric is not None:
        metric.labels(activity_type=activity_type, outcome=outcome).inc()


def record_task_duration(workflow_type: str, seconds: float) -> None:
    metric = _metric("kinora_workflow_task_duration_seconds")
    if metric is not None:
        metric.labels(workflow_type=workflow_type).observe(seconds)


def record_nondeterminism(workflow_type: str) -> None:
    metric = _metric("kinora_workflow_nondeterminism_total")
    if metric is not None:
        metric.labels(workflow_type=workflow_type).inc()


def record_timers_fired(count: int) -> None:
    metric = _metric("kinora_workflow_timers_fired_total")
    if metric is not None and count:
        metric.inc(count)


def set_open_executions(count: int) -> None:
    metric = _metric("kinora_workflow_open_executions")
    if metric is not None:
        metric.set(count)


__all__ = [
    "record_activity_outcome",
    "record_execution_terminal",
    "record_nondeterminism",
    "record_task_duration",
    "record_timers_fired",
    "set_open_executions",
]
