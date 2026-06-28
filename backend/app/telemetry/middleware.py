"""Drop-in ASGI middleware + logging integration for the telemetry layer.

This module packages the request-time wiring so adopting it is a *single additive
line* — no edits to the existing request middleware in ``app.main`` are required.
Two pieces:

* :class:`CorrelationMiddleware` — a Starlette ``BaseHTTPMiddleware`` that, per
  request, derives a correlation id (honouring an inbound ``X-Correlation-Id`` /
  ``traceparent`` so a trace continues across the gateway), opens a RED-tracked
  request span, binds the ids for the whole handler (so every log line is
  correlated), and echoes ``X-Correlation-Id`` back on the response.
* :func:`install_correlation_logging` — splices the correlation structlog
  processor into the configured chain so the bound ids surface in log output.

Both are import-safe and offline-safe. To adopt, add in ``create_app``::

    from app.telemetry.middleware import CorrelationMiddleware, install_correlation_logging
    install_correlation_logging()
    app.add_middleware(CorrelationMiddleware)

The existing ``_record_requests`` middleware (the ``kinora_http_requests_total``
counter) is unaffected — this adds the duration histogram + span + correlation id
alongside it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger
from app.telemetry import context as ctx
from app.telemetry import red
from app.telemetry.spans import extract_context

logger = get_logger("app.telemetry.middleware")

#: Response header the resolved correlation id is echoed back on.
CORRELATION_HEADER = "X-Correlation-Id"


def _route_template(request: Request) -> str:
    """Resolve the matched route template (bounded label), else a fallback.

    Mirrors ``app.main._metric_path``: never use the raw path as a label, so a
    crawler can't mint unbounded time series.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return request.url.path if request.url.path else "<unknown>"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id + open a RED request span around every request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Continue an inbound trace/correlation if the caller supplied one.
        extracted = extract_context(dict(request.headers))
        method = request.method
        # The route is only known after matching; BaseHTTPMiddleware runs before
        # routing, so use the URL path here and let RED record the template later.
        path = request.url.path

        with red.track_request(method, path, correlation_id=extracted.correlation_id) as rc:
            if extracted.valid:
                # Adopt the remote trace so handler spans/log lines join it.
                ctx.set_trace_context(extracted.trace_id, extracted.span_id)  # type: ignore[arg-type]
            correlation_id = ctx.get_correlation_id()
            try:
                response = await call_next(request)
            except Exception:
                rc["status"] = 500
                raise
            rc["status"] = response.status_code
            if correlation_id:
                response.headers.setdefault(CORRELATION_HEADER, correlation_id)
            return response


def install_correlation_logging() -> None:
    """Splice the correlation processor into the active structlog chain.

    Idempotent: if the processor is already present the chain is left untouched.
    Safe to call at app startup right after :func:`app.core.logging.configure_logging`.
    """
    try:
        config = structlog.get_config()
        processors = list(config.get("processors", []))
    except Exception as exc:  # noqa: BLE001 - logging wiring must never break startup
        logger.debug("telemetry.logging_inspect_failed", error=str(exc))
        return
    if ctx.merge_correlation in processors:
        return
    # Insert just before the final renderer so ids are present at render time.
    insert_at = max(0, len(processors) - 1)
    processors.insert(insert_at, ctx.merge_correlation)
    try:
        structlog.configure(processors=processors)
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry.logging_configure_failed", error=str(exc))


__all__ = [
    "CORRELATION_HEADER",
    "CorrelationMiddleware",
    "install_correlation_logging",
]
