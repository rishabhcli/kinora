"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.realtime.routes_realtime import router as realtime_router
from app.api.routes import (
    auth,
    books,
    director,
    events,
    films,
    library,
    metrics,
    optim,
    prefs,
    sessions,
)

#: The routers mounted (in order) under the versioned ``/api`` prefix.
ROUTERS = [
    auth.router,
    books.router,
    films.router,  # A3: /books/{id}/events + /scenes/{id}/film (Captain-registered)
    library.router,  # A5: library catalog (Captain-registered on merge)
    sessions.router,
    director.router,
    prefs.router,
    events.router,
    metrics.router,
    optim.router,
    # Realtime + API-quality layer (resumable SSE/WS, presence, cursor
    # pagination, versions). Additive — extends the §5.6 transport without
    # touching the round-1 event/session/director routes.
    realtime_router,
]

__all__ = ["ROUTERS"]
