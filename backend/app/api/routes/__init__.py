"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

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

# Additive: the content-moderation & safety admin/operations surface (§9/§10).
# The router lives under app.moderation to keep the safety domain self-contained.
from app.moderation.routes import router as moderation_router

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
    moderation_router,  # content moderation & safety (§9/§10)
]

__all__ = ["ROUTERS"]
