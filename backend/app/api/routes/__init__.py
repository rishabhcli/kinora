"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.routes import (
    auth,
    books,
    director,
    events,
    library,
    metrics,
    prefs,
    sessions,
)

#: The routers mounted (in order) under the versioned ``/api`` prefix.
ROUTERS = [
    auth.router,
    books.router,
    library.router,
    sessions.router,
    director.router,
    prefs.router,
    events.router,
    metrics.router,
]

__all__ = ["ROUTERS"]
