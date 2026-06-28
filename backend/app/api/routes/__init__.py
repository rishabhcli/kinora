"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.routes import (
    auth,
    books,
    director,
    events,
    films,
    integrations,
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
    integrations.router,  # third-party source import (app.integrations)
]

__all__ = ["ROUTERS"]
