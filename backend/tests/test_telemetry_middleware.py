"""Telemetry: the drop-in correlation/RED ASGI middleware + logging splice.

Exercised against a throwaway FastAPI app (no Kinora infra needed) so the
middleware's request-time behaviour is asserted in isolation.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.telemetry import context as ctx
from app.telemetry import spans
from app.telemetry.exporters import InMemorySpanExporter
from app.telemetry.middleware import (
    CORRELATION_HEADER,
    CorrelationMiddleware,
    install_correlation_logging,
)


def _app() -> tuple[FastAPI, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    spans.set_tracer(spans.Tracer(exporter=exporter))
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str | None]:
        # The handler sees a bound correlation id and trace.
        return {"correlation_id": ctx.get_correlation_id(), "trace_id": ctx.get_trace_id()}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise ValueError("explode")

    return app, exporter


async def test_middleware_binds_and_echoes_correlation_id() -> None:
    app, exporter = _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/ping")
    assert resp.status_code == 200
    body = resp.json()
    assert body["correlation_id"] is not None
    assert body["trace_id"] is not None
    # The resolved id is echoed back to the caller.
    assert resp.headers[CORRELATION_HEADER] == body["correlation_id"]
    # A request span was recorded.
    assert any(s.name == "http.request" for s in exporter.finished_spans())


async def test_middleware_continues_inbound_trace() -> None:
    app, exporter = _app()
    trace_id = "a" * 32
    span_id = "b" * 16
    carrier = {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "x-correlation-id": "corr_inbound",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/ping", headers=carrier)
    body = resp.json()
    assert body["correlation_id"] == "corr_inbound"
    assert body["trace_id"] == trace_id
    assert resp.headers[CORRELATION_HEADER] == "corr_inbound"


async def test_middleware_records_error_span_on_exception() -> None:
    app, exporter = _app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/boom")
    assert resp.status_code == 500
    request_span = next(s for s in exporter.finished_spans() if s.name == "http.request")
    assert request_span.status == spans.STATUS_ERROR


def test_install_correlation_logging_is_idempotent() -> None:
    # Configure a minimal chain, then splice — twice. The processor appears once.
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    install_correlation_logging()
    install_correlation_logging()
    procs = structlog.get_config()["processors"]
    assert procs.count(ctx.merge_correlation) == 1
    # Restore the app's real logging config so sibling tests are unaffected.
    from app.core.logging import configure_logging

    configure_logging("INFO")
