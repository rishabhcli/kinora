"""FastAPI application factory + the composition-root lifespan.

``create_app()`` builds the app with its liveness (``/health``), readiness
(``/ready``), and Prometheus (``/metrics``) endpoints, mounts every domain
router under ``/api``, installs the typed error handlers, and on startup builds
the wired :class:`app.composition.Container` into ``app.state`` and launches the
background **idle-sweeper** (cancels speculative work 8s after a reader goes
quiet, per §4.7). Construction is lazy, so ``create_app()`` still imports and
serves ``/health`` with ``DASHSCOPE_API_KEY=test`` and no network.

A test (or an alternate entrypoint) may pre-set ``app.state.container`` and
``app.state.run_idle_sweeper`` before the lifespan runs to inject a container
built against throwaway infrastructure and to suppress the sweeper.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.errors import install_exception_handlers
from app.api.middleware import SecurityHeadersMiddleware
from app.api.routes import ROUTERS
from app.auth.middleware import CsrfMiddleware
from app.composition import Container, build_container
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.observability.metrics import record_request, render_metrics, set_app_info
from app.observability.tracing import init_tracing

#: API version prefix every domain router is mounted under.
API_PREFIX = "/api"
#: Idle-sweeper cadence — short enough to catch the 8s idle-pause promptly (§4.7).
IDLE_SWEEP_INTERVAL_S = 4.0
#: Redis key prefix the Scheduler stores session control state under.
_SESSION_KEY_PREFIX = "kinora:sched:session"


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


async def _idle_sweeper(container: Container, stop: asyncio.Event) -> None:
    """Periodically idle-pause quiet sessions (cancel speculative work, §4.7).

    Scans the Scheduler's Redis session keys and runs one idle-aware tick per
    session; :meth:`IntentController.sweep_idle` only cancels speculation when a
    reader has been quiet for ≥ 8s. Fully defensive: a Redis blip is logged and
    retried on the next cadence, never crashing the app.
    """
    logger = get_logger("app.main.idle_sweeper")
    match = f"{_SESSION_KEY_PREFIX}:*"
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=IDLE_SWEEP_INTERVAL_S)
        if stop.is_set():
            break
        try:
            session_ids = await _scan_session_ids(container, match)
            for session_id in session_ids:
                async with container.session_factory() as session:
                    controller = container.build_intent_controller(session)
                    await controller.sweep_idle(session_id)
        except Exception as exc:  # noqa: BLE001 - the sweeper must never crash the app
            logger.warning("idle_sweeper.error", error=str(exc))


async def _scan_session_ids(container: Container, match: str) -> list[str]:
    raw = container.redis.raw
    cursor = 0
    out: list[str] = []
    prefix_len = len(_SESSION_KEY_PREFIX) + 1
    while True:
        cursor, batch = await raw.scan(cursor=cursor, match=match, count=200)
        out.extend(key[prefix_len:] for key in batch)
        if cursor == 0:
            break
    return out


def create_app() -> FastAPI:
    """Build and configure the Kinora FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("app.main")
    started_at = time.monotonic()

    def _uptime_seconds() -> float:
        return round(time.monotonic() - started_at, 3)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = getattr(app.state, "container", None)
        if container is None:
            container = build_container(settings)
            app.state.container = container
        await container.startup()
        logger.info(
            "kinora.startup",
            service=settings.service_name,
            env=settings.app_env,
            version=__version__,
        )
        stop = asyncio.Event()
        sweeper: asyncio.Task[None] | None = None
        if getattr(app.state, "run_idle_sweeper", True):
            sweeper = asyncio.create_task(_idle_sweeper(container, stop))
        try:
            yield
        finally:
            stop.set()
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sweeper
            await container.shutdown()
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
    # Security headers on every response (HSTS only outside local, §12).
    app.add_middleware(SecurityHeadersMiddleware, hsts=not settings.is_local)
    # CSRF double-submit guard for cookie-authenticated writes (§12). A no-op for
    # the Bearer/API-key callers Kinora actually uses, so it adds defence in depth
    # without affecting the desktop app or headless integrations.
    app.add_middleware(
        CsrfMiddleware,
        enabled=settings.csrf_enabled,
        cookie_name=settings.csrf_cookie_name,
        header_name=settings.csrf_header_name,
        secure_cookie=not settings.is_local,
    )

    set_app_info(service=settings.service_name, version=__version__, env=settings.app_env)
    install_exception_handlers(app)
    # Optional OTel tracing — a clean no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
    init_tracing(app, service_name=settings.service_name)

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
                "api": API_PREFIX,
            },
        }

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    async def health() -> dict[str, object]:
        return {"status": "ok", **_service_info(), "uptime_seconds": _uptime_seconds()}

    @app.get("/ready", tags=["meta"], summary="Readiness probe")
    async def ready(request: Request) -> Response:
        # Readiness actively probes the critical dependencies (Postgres SELECT 1 +
        # Redis PING) and answers 503 when any is down, so a load balancer / k8s
        # readiness gate stops routing traffic to an instance that cannot serve.
        # /health stays a pure liveness check.
        info: dict[str, object] = {
            "status": "ready",
            **_service_info(),
            "uptime_seconds": _uptime_seconds(),
        }
        container = getattr(request.app.state, "container", None)
        probe = getattr(container, "check_readiness", None)
        if probe is None:
            return JSONResponse(info)
        checks = await probe()
        info["checks"] = checks
        if all(checks.values()):
            return JSONResponse(info)
        info["status"] = "not_ready"
        return JSONResponse(info, status_code=503)

    @app.get("/metrics", tags=["meta"], summary="Prometheus exposition")
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    return app


app = create_app()
