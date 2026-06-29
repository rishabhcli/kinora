"""RPC mesh metrics — additive series on the shared Prometheus registry.

Registers ``kinora_rpc_*`` series against the *existing* shared
:data:`app.observability.metrics.registry` (no edit to that module — purely
additive), so the mesh's golden signals show up on the same ``/metrics`` scrape
as the render/queue/provider telemetry. The series cover what an SRE needs to run
a mesh: call volume + outcome by endpoint, end-to-end latency, retries / hedges
fired, circuit-breaker state transitions, and per-endpoint in-flight depth.

Emit through the tiny typed helpers so call sites never import prometheus types.
Registration is wrapped so importing this module twice (test reload) does not
raise a duplicate-registration error — it falls back to looking the existing
collector up by name.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from app.observability.metrics import _LATENCY_BUCKETS, registry


def _counter(name: str, doc: str, labelnames: tuple[str, ...] = ()) -> Counter:
    try:
        return Counter(name, doc, labelnames=labelnames, registry=registry)
    except ValueError:
        return registry._names_to_collectors[name]  # type: ignore[return-value]


def _gauge(name: str, doc: str, labelnames: tuple[str, ...] = ()) -> Gauge:
    try:
        return Gauge(name, doc, labelnames=labelnames, registry=registry)
    except ValueError:
        return registry._names_to_collectors[name]  # type: ignore[return-value]


def _histogram(name: str, doc: str, labelnames: tuple[str, ...], buckets: Any) -> Histogram:
    try:
        return Histogram(name, doc, labelnames=labelnames, buckets=buckets, registry=registry)
    except ValueError:
        return registry._names_to_collectors[name]  # type: ignore[return-value]


rpc_calls_total: Counter = _counter(
    "kinora_rpc_calls_total",
    "Total internal RPC calls by endpoint + terminal status code.",
    ("service", "method", "code"),
)

rpc_latency_seconds: Histogram = _histogram(
    "kinora_rpc_latency_seconds",
    "End-to-end latency of an internal RPC call (incl. retries/hedges).",
    ("service", "method"),
    _LATENCY_BUCKETS,
)

rpc_retries_total: Counter = _counter(
    "kinora_rpc_retries_total",
    "RPC retry attempts fired (beyond the first attempt) by endpoint.",
    ("service", "method"),
)

rpc_hedges_total: Counter = _counter(
    "kinora_rpc_hedges_total",
    "RPC hedge requests fired (backup copies) by endpoint.",
    ("service", "method"),
)

rpc_circuit_transitions_total: Counter = _counter(
    "kinora_rpc_circuit_transitions_total",
    "Circuit-breaker state transitions by endpoint + new state.",
    ("endpoint", "state"),
)

rpc_circuit_rejections_total: Counter = _counter(
    "kinora_rpc_circuit_rejections_total",
    "Calls fast-failed because the circuit breaker was open, by endpoint.",
    ("endpoint",),
)

rpc_inflight: Gauge = _gauge(
    "kinora_rpc_inflight",
    "Currently in-flight internal RPC calls by endpoint.",
    ("service", "method"),
)

rpc_deadline_exceeded_total: Counter = _counter(
    "kinora_rpc_deadline_exceeded_total",
    "RPC calls that ran out of their deadline budget, by endpoint.",
    ("service", "method"),
)


def observe_rpc(service: str, method: str, *, code: str, latency_s: float) -> None:
    """Record one terminal RPC call (its status code + end-to-end latency)."""
    rpc_calls_total.labels(service=service, method=method, code=code).inc()
    rpc_latency_seconds.labels(service=service, method=method).observe(max(latency_s, 0.0))


def inc_rpc_retry(service: str, method: str, count: int = 1) -> None:
    """Count ``count`` retry attempts fired for an endpoint."""
    if count > 0:
        rpc_retries_total.labels(service=service, method=method).inc(count)


def inc_rpc_hedge(service: str, method: str, count: int = 1) -> None:
    """Count ``count`` hedge requests fired for an endpoint."""
    if count > 0:
        rpc_hedges_total.labels(service=service, method=method).inc(count)


def inc_circuit_transition(endpoint: str, state: str) -> None:
    """Count one circuit-breaker transition into ``state`` for an endpoint."""
    rpc_circuit_transitions_total.labels(endpoint=endpoint, state=state).inc()


def inc_circuit_rejection(endpoint: str) -> None:
    """Count one call fast-failed by an open breaker."""
    rpc_circuit_rejections_total.labels(endpoint=endpoint).inc()


def inc_deadline_exceeded(service: str, method: str) -> None:
    """Count one call that exhausted its deadline budget."""
    rpc_deadline_exceeded_total.labels(service=service, method=method).inc()


def track_inflight(service: str, method: str) -> _InFlightScope:
    """A context manager that inc/decrements the in-flight gauge for an endpoint."""
    return _InFlightScope(service, method)


class _InFlightScope:
    """inc on enter, dec on exit — even when the call raises."""

    __slots__ = ("_g",)

    def __init__(self, service: str, method: str) -> None:
        self._g = rpc_inflight.labels(service=service, method=method)

    def __enter__(self) -> _InFlightScope:
        self._g.inc()
        return self

    def __exit__(self, *exc: object) -> None:
        self._g.dec()


__all__ = [
    "inc_circuit_rejection",
    "inc_circuit_transition",
    "inc_deadline_exceeded",
    "inc_rpc_hedge",
    "inc_rpc_retry",
    "observe_rpc",
    "track_inflight",
]
