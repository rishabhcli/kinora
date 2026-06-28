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
    recommendations,
    reports,
    search,
    sessions,
    translation,
    workspaces,
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
    recommendations.router,  # server-side recsys: watch-next + interaction logging
    reports.router,  # reports subsystem: document generation + signed retrieval
    integrations.router,  # third-party source import (app.integrations)
    workspaces.router,  # Workspaces & teams: collaboration ownership (§5)
    search.router,  # server-side search engine: /search, /search/suggest, /search/reindex
    translation.router,  # content-translation subsystem (app.translation, §8/§9)
    moderation_router,  # content moderation & safety (§9/§10)
]


def root_routers() -> list:
    """Routers mounted at the application *root* (no ``/api`` prefix).

    The public GraphQL gateway is its own self-contained surface at ``/graphql``,
    deliberately separate from the internal REST API (additive — see
    ``app/graphql/DESIGN.md``). Imported lazily so the GraphQL package's schema is
    assembled only when this is called, keeping ``app.api.routes`` import cheap.
    """
    from app.graphql.app import router as graphql_router

    return [graphql_router]


__all__ = ["ROUTERS", "root_routers"]
