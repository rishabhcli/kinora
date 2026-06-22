"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.routes import auth, books, director, events, metrics, sessions

#: The routers mounted (in order) under the versioned ``/api`` prefix.
ROUTERS = [
    auth.router,
    books.router,
    sessions.router,
    director.router,
    events.router,
    metrics.router,
]

__all__ = ["ROUTERS"]
