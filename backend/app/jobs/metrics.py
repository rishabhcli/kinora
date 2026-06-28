"""Prometheus metrics for the jobs framework (kinora.md §12.5, jobs flavour).

Registers a small set of ``kinora_jobs_*`` series against the *shared* metrics
registry from :mod:`app.observability.metrics`, so they appear on the existing
``/metrics`` endpoint without touching that module. Cardinality is bounded: the
per-job label is the registered job *name* (a small, fixed set), never a run id.

The dispatcher and the loops call the tiny emit helpers here; metrics never leak
Prometheus types into the framework logic. Registration is guarded so importing
this module twice during tests cannot raise a duplicate-registration error.
"""

from __future__ import annotations

import contextlib

from prometheus_client import Counter, Gauge, Histogram

from app.observability.metrics import registry

# Guard against duplicate registration (re-import under test reloaders).
with contextlib.suppress(ValueError):
    _JOB_RUNS = Counter(
        "kinora_jobs_runs_total",
        "Total job runs by job name and terminal decision.",
        labelnames=("job", "decision"),
        registry=registry,
    )
    _JOB_DURATION = Histogram(
        "kinora_jobs_run_duration_seconds",
        "Handler execution wall-clock duration, by job name.",
        labelnames=("job",),
        buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0, 600.0),
        registry=registry,
    )
    _JOB_RETRIES = Counter(
        "kinora_jobs_retries_total",
        "Total job retries scheduled, by job name.",
        labelnames=("job",),
        registry=registry,
    )
    _JOB_DEADLETTERS = Counter(
        "kinora_jobs_deadletters_total",
        "Total job runs dead-lettered, by job name.",
        labelnames=("job",),
        registry=registry,
    )
    _JOB_ACTIVE = Gauge(
        "kinora_jobs_active_runs",
        "Current number of active (pending/running/retrying) runs.",
        registry=registry,
    )
    _LEADER = Gauge(
        "kinora_jobs_leader",
        "1 when this node holds the scheduling leader lease, else 0.",
        registry=registry,
    )


def _counter(name: str) -> Counter | None:
    return globals().get(name)


def inc_run(job: str, decision: str) -> None:
    """Count one terminal run decision (completed/retry/deadletter)."""
    metric = _counter("_JOB_RUNS")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.labels(job=job, decision=decision).inc()


def observe_duration(job: str, seconds: float) -> None:
    """Record handler wall-clock duration for ``job``."""
    metric = globals().get("_JOB_DURATION")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.labels(job=job).observe(max(0.0, seconds))


def inc_retry(job: str) -> None:
    """Count one scheduled retry for ``job``."""
    metric = _counter("_JOB_RETRIES")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.labels(job=job).inc()


def inc_deadletter(job: str) -> None:
    """Count one dead-letter for ``job``."""
    metric = _counter("_JOB_DEADLETTERS")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.labels(job=job).inc()


def set_active(count: int) -> None:
    """Set the active-runs gauge (called from a periodic stats snapshot)."""
    metric = globals().get("_JOB_ACTIVE")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.set(count)


def set_leader(is_leader: bool) -> None:
    """Set the leadership gauge for this node."""
    metric = globals().get("_LEADER")
    if metric is not None:
        with contextlib.suppress(Exception):
            metric.set(1 if is_leader else 0)


__all__ = [
    "inc_deadletter",
    "inc_retry",
    "inc_run",
    "observe_duration",
    "set_active",
    "set_leader",
]
