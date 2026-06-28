"""RED metrics for the API surface — Rate, Errors, Duration.

The RED method instruments a *request-driven* service by three signals:

* **Rate** — requests handled per second (a counter);
* **Errors** — the fraction that failed (5xx / unhandled);
* **Duration** — the latency distribution (a histogram).

The observability package already owns the request counter
(``kinora_http_requests_total``). This module adds the **duration** histogram and
a single ergonomic context manager / decorator (:func:`track_request`) that an
endpoint or the request middleware wraps work in to emit all three at once —
plus it opens a span so the request shows up in the trace tree with its
correlation id bound.

Cardinality is bounded: the ``path`` label must be a *route template*
(``/api/sessions/{id}``), never a raw URL, exactly as the existing middleware
resolves it (``app.main._metric_path``).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any

from app.telemetry import context as ctx
from app.telemetry.spans import STATUS_ERROR, span

# Latency buckets tuned for an API (sub-second mostly, a long tail for SSE/setup).
_API_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_registered = False
_lock = threading.Lock()
_duration: Any | None = None
_in_flight: Any | None = None


def _ensure_registered() -> bool:
    """Register the RED duration histogram + in-flight gauge (idempotent)."""
    global _registered, _duration, _in_flight
    if _registered:
        return _duration is not None
    with _lock:
        if _registered:
            return _duration is not None
        _registered = True
        try:
            from prometheus_client import Gauge, Histogram

            from app.observability.metrics import registry

            _duration = Histogram(
                "kinora_http_request_duration_seconds",
                "API request latency (RED: Duration), by method/path/status.",
                labelnames=("method", "path", "status"),
                buckets=_API_LATENCY_BUCKETS,
                registry=registry,
            )
            _in_flight = Gauge(
                "kinora_http_requests_in_flight",
                "API requests currently being served (RED: saturation signal).",
                labelnames=("method", "path"),
                registry=registry,
            )
        except Exception:  # noqa: BLE001 - degrade to span-only when prom absent
            _duration = None
            _in_flight = None
    return _duration is not None


def observe_request_duration(method: str, path: str, status: int, seconds: float) -> None:
    """Record one request's latency in the RED duration histogram."""
    if not _ensure_registered() or _duration is None:
        return
    with contextlib.suppress(Exception):
        _duration.labels(method=method, path=path, status=str(status)).observe(max(0.0, seconds))


@contextlib.contextmanager
def track_request(
    method: str,
    path: str,
    *,
    correlation_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Instrument one API request: RED duration + in-flight + a trace span.

    Binds a correlation id for the duration of the request (so every log line in
    the handler is correlated) and yields a small mutable dict; set
    ``ctx["status"]`` to the response status before exit so the right label is
    used. Defaults the status to 500 so an unhandled exception is counted as an
    error even if the handler never set it.
    """
    _ensure_registered()
    request_ctx: dict[str, Any] = {"status": 500}
    tokens = ctx.bind_correlation_id(correlation_id)
    if _in_flight is not None:
        with contextlib.suppress(Exception):
            _in_flight.labels(method=method, path=path).inc()
    started = time.monotonic()
    try:
        with span("http.request", attributes={"http.method": method, "http.route": path}) as sp:
            try:
                yield request_ctx
            finally:
                status = int(request_ctx.get("status", 500))
                sp.set_attribute("http.status_code", status)
                if status >= 500:
                    sp.set_status(STATUS_ERROR)
    finally:
        elapsed = time.monotonic() - started
        status = int(request_ctx.get("status", 500))
        observe_request_duration(method, path, status, elapsed)
        if _in_flight is not None:
            with contextlib.suppress(Exception):
                _in_flight.labels(method=method, path=path).dec()
        ctx.reset_context(tokens)


__all__ = ["observe_request_duration", "track_request"]
