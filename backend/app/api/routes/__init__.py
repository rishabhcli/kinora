"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.routes import (
    auth,
    books,
    director,
    events,
    films,
    library,
    llmops,
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
    llmops.router,  # LLM-ops surface (prompt registry/eval/guardrails); 404 unless llmops_enabled
]

__all__ = ["ROUTERS"]
