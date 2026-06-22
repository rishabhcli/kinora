"""Prometheus metrics.

Uses a dedicated :class:`~prometheus_client.CollectorRegistry` (not the global
default) so metrics are isolated and re-importing the module during tests cannot
trigger duplicate-registration errors. Phase-specific series (per-shot latency,
video-seconds, buffer occupancy, ...) are added in later phases.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

#: Process-wide registry that all Kinora metrics register against.
registry: CollectorRegistry = CollectorRegistry(auto_describe=True)

http_requests_total: Counter = Counter(
    "kinora_http_requests_total",
    "Total number of HTTP requests handled, labelled by method/path/status.",
    labelnames=("method", "path", "status"),
    registry=registry,
)

app_info: Gauge = Gauge(
    "kinora_app_info",
    "Static application info; value is always 1, metadata carried in labels.",
    labelnames=("service", "version", "env"),
    registry=registry,
)


def set_app_info(*, service: str, version: str, env: str) -> None:
    """Set the single ``app_info`` series describing this process."""
    app_info.labels(service=service, version=version, env=env).set(1)


def record_request(method: str, path: str, status: int) -> None:
    """Increment the request counter for a completed HTTP request."""
    http_requests_total.labels(method=method, path=path, status=str(status)).inc()


def render_metrics() -> tuple[bytes, str]:
    """Return the exposition payload and its content type for ``/metrics``."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
