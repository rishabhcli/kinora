"""Flag-gated Prometheus exposition (§12.5).

``app/main.py`` already serves a fixed ``/metrics`` route. This module is the
*composable* surface the observability plane uses to mount exposition **behind a
flag**: when ``observability_metrics_enabled`` is off (e.g. a packaged build that
must not expose an unauthenticated scrape surface), :func:`build_metrics_router`
returns ``None`` and nothing is mounted.

The router itself is a thin wrapper over
:func:`app.observability.metrics.render_metrics`; it stays import-safe (FastAPI is
already a hard dependency) and never scrapes a collector.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.observability.metrics import render_metrics

#: Default path the Prometheus exposition is served at.
DEFAULT_METRICS_PATH = "/metrics"


def build_metrics_router(*, enabled: bool, path: str = DEFAULT_METRICS_PATH) -> APIRouter | None:
    """Return a router exposing the Prometheus scrape, or ``None`` when disabled.

    The single ``GET {path}`` returns the exposition payload with the Prometheus
    content type. When ``enabled`` is ``False`` this returns ``None`` so the caller
    mounts nothing — the flag fully gates the surface.
    """
    if not enabled:
        return None

    router = APIRouter(tags=["meta"])

    @router.get(path, summary="Prometheus exposition", include_in_schema=False)
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    return router


__all__ = ["DEFAULT_METRICS_PATH", "build_metrics_router"]
