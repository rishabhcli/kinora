"""FastAPI application factory.

Exposes liveness (``/health``), readiness (``/ready``), and Prometheus
(``/metrics``) endpoints. Domain routers (auth, books, sessions, the SSE/WS
event stream) are mounted in the API-gateway phase.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.observability.metrics import record_request, render_metrics, set_app_info


def _metric_path(request: Request) -> str:
    """Resolve a bounded, low-cardinality path label for a request.

    Prometheus labels must not take unbounded values: using the raw URL path
    lets any crawler or attacker mint a new time series per random URL. We map
    each request to its matched *route template* (e.g. ``/health``); requests
    that match no route collapse into a single ``"<unmatched>"`` series.
    """
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return "<unmatched>"
    for route in request.app.routes:
        if getattr(route, "endpoint", None) is endpoint:
            return getattr(route, "path", "<unknown>")
    return "<unknown>"


def create_app() -> FastAPI:
    """Build and configure the Kinora FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("app.main")
    started_at = time.monotonic()

    def _uptime_seconds() -> float:
        return round(time.monotonic() - started_at, 3)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "kinora.startup",
            service=settings.service_name,
            env=settings.app_env,
            version=__version__,
        )
        yield
        logger.info("kinora.shutdown", service=settings.service_name)

    app = FastAPI(
        title="Kinora API",
        version=__version__,
        summary="Generation-on-scroll showrunner backend.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    set_app_info(service=settings.service_name, version=__version__, env=settings.app_env)

    @app.middleware("http")
    async def _record_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        record_request(request.method, _metric_path(request), response.status_code)
        return response

    def _service_info() -> dict[str, object]:
        return {
            "service": settings.service_name,
            "version": __version__,
            "environment": settings.app_env,
        }

    @app.get("/", tags=["meta"], summary="Service root and endpoint index")
    async def root() -> dict[str, object]:
        return {
            "name": "Kinora API",
            "status": "ok",
            "message": "Kinora — watch the book. Generation-on-scroll showrunner backend.",
            **_service_info(),
            "docs": "/docs",
            "endpoints": {
                "health": "/health",
                "ready": "/ready",
                "metrics": "/metrics",
                "docs": "/docs",
                "openapi": "/openapi.json",
            },
        }

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    async def health() -> dict[str, object]:
        return {"status": "ok", **_service_info(), "uptime_seconds": _uptime_seconds()}

    @app.get("/ready", tags=["meta"], summary="Readiness probe")
    async def ready() -> dict[str, object]:
        # Dependency checks (Postgres, Redis, object storage) are wired in the
        # data-layer phase; until then readiness mirrors liveness.
        return {"status": "ready", **_service_info(), "uptime_seconds": _uptime_seconds()}

    @app.get("/metrics", tags=["meta"], summary="Prometheus exposition")
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    return app


app = create_app()
