"""USE metrics for the workers + render queue — Utilization, Saturation, Errors.

The USE method instruments a *resource-driven* service (here the render-worker
draining the Redis priority queue) by three signals:

* **Utilization** — the fraction of time the resource is busy (worker busy ratio,
  derived from job-processing time / wall-clock);
* **Saturation** — how much work is queued and waiting (queue depth per lane);
* **Errors** — failed / dead-lettered jobs.

The observability package already owns ``kinora_queue_depth``,
``kinora_jobs_total`` and ``kinora_dlq_total``; this module adds the
**utilization** gauge + a job-duration histogram and a single ergonomic
:func:`track_job` context manager the worker wraps each job in. It also continues
any cross-process trace carried on the job (W3C ``traceparent``), so a render job
shares the trace of the request that enqueued it.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.telemetry import context as ctx
from app.telemetry.spans import STATUS_ERROR, adopt_remote_context, span

# Job latency buckets — a render job is much longer than an API request.
_JOB_LATENCY_BUCKETS: tuple[float, ...] = (
    0.1,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

_registered = False
_lock = threading.Lock()
_busy_ratio: Any | None = None
_job_duration: Any | None = None
_jobs_in_flight: Any | None = None


def _ensure_registered() -> bool:
    global _registered, _busy_ratio, _job_duration, _jobs_in_flight
    if _registered:
        return _job_duration is not None
    with _lock:
        if _registered:
            return _job_duration is not None
        _registered = True
        try:
            from prometheus_client import Gauge, Histogram

            from app.observability.metrics import registry

            _busy_ratio = Gauge(
                "kinora_worker_busy_ratio",
                "Worker utilization: fraction of recent wall-clock spent on jobs.",
                labelnames=("worker",),
                registry=registry,
            )
            _job_duration = Histogram(
                "kinora_job_duration_seconds",
                "Render-job processing time (USE: per-lane), by lane/outcome.",
                labelnames=("lane", "outcome"),
                buckets=_JOB_LATENCY_BUCKETS,
                registry=registry,
            )
            _jobs_in_flight = Gauge(
                "kinora_jobs_in_flight",
                "Jobs currently being processed (USE: saturation of the workers).",
                labelnames=("worker",),
                registry=registry,
            )
        except Exception:  # noqa: BLE001
            _busy_ratio = _job_duration = _jobs_in_flight = None
    return _job_duration is not None


@dataclass(slots=True)
class _BusyTracker:
    """Sliding busy-ratio estimate per worker (EWMA-free, windowed sum)."""

    window_s: float = 30.0
    busy_s: float = 0.0
    window_start: float = 0.0

    def add_busy(self, seconds: float, *, now: float) -> float:
        if self.window_start == 0.0:
            self.window_start = now
        self.busy_s += max(0.0, seconds)
        elapsed = now - self.window_start
        if elapsed >= self.window_s:
            ratio = min(1.0, self.busy_s / elapsed) if elapsed > 0 else 0.0
            self.busy_s = 0.0
            self.window_start = now
            return ratio
        return min(1.0, self.busy_s / elapsed) if elapsed > 0 else 0.0


_busy_trackers: dict[str, _BusyTracker] = {}
_busy_lock = threading.Lock()


def observe_job_duration(lane: str, outcome: str, seconds: float) -> None:
    """Record one job's processing time (USE: per-lane duration)."""
    if not _ensure_registered() or _job_duration is None:
        return
    with contextlib.suppress(Exception):
        _job_duration.labels(lane=lane, outcome=outcome).observe(max(0.0, seconds))


def set_worker_busy_ratio(worker: str, ratio: float) -> None:
    """Set a worker's utilization gauge directly (USE: utilization)."""
    if not _ensure_registered() or _busy_ratio is None:
        return
    with contextlib.suppress(Exception):
        _busy_ratio.labels(worker=worker).set(max(0.0, min(1.0, ratio)))


@contextlib.contextmanager
def track_job(
    lane: str,
    *,
    worker: str = "render-worker",
    carrier: Mapping[str, str] | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Instrument one render job: USE duration/utilization + a continued trace.

    If ``carrier`` carries a W3C ``traceparent`` (stamped by the enqueuing request
    via :func:`app.telemetry.spans.inject_context`), the job's span continues that
    trace and shares its correlation id. Yields a small mutable dict; set
    ``ctx["outcome"]`` (``succeeded`` / ``failed`` / ``deadletter`` / ``cancelled``)
    before exit. An unhandled exception is recorded as ``failed``.
    """
    _ensure_registered()
    job_ctx: dict[str, Any] = {"outcome": "failed"}
    remote_tokens = adopt_remote_context(carrier) if carrier else None
    attrs: dict[str, Any] = {"job.lane": lane, "worker": worker}
    if attributes:
        attrs.update(attributes)
    if _jobs_in_flight is not None:
        with contextlib.suppress(Exception):
            _jobs_in_flight.labels(worker=worker).inc()
    started = time.monotonic()
    try:
        with span("job.render", attributes=attrs) as sp:
            try:
                yield job_ctx
            except BaseException:
                sp.set_status(STATUS_ERROR)
                raise
            finally:
                outcome = str(job_ctx.get("outcome", "failed"))
                sp.set_attribute("job.outcome", outcome)
                if outcome in {"failed", "deadletter"}:
                    sp.set_status(STATUS_ERROR)
    finally:
        elapsed = time.monotonic() - started
        outcome = str(job_ctx.get("outcome", "failed"))
        observe_job_duration(lane, outcome, elapsed)
        now = time.monotonic()
        with _busy_lock:
            tracker = _busy_trackers.setdefault(worker, _BusyTracker())
            ratio = tracker.add_busy(elapsed, now=now)
        set_worker_busy_ratio(worker, ratio)
        if _jobs_in_flight is not None:
            with contextlib.suppress(Exception):
                _jobs_in_flight.labels(worker=worker).dec()
        if remote_tokens is not None:
            ctx.reset_context(remote_tokens)


__all__ = [
    "observe_job_duration",
    "set_worker_busy_ratio",
    "track_job",
]
