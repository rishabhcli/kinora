"""FastAPI application factory.

Exposes liveness (``/health``), readiness (``/ready``), and Prometheus
(``/metrics``) endpoints. Domain routers (auth, books, sessions, the SSE/WS
event stream) are mounted in the API-gateway phase.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.observability.metrics import record_request, render_metrics, set_app_info


def create_app() -> FastAPI:
    """Build and configure the Kinora FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("app.main")

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
        record_request(request.method, request.url.path, response.status_code)
        return response

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["meta"])
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/metrics", tags=["meta"])
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    return app


app = create_app()
