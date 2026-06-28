"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.realtime.routes_realtime import router as realtime_router
from app.api.routes import (
    analytics,
    assistant,
    auth,
    books,
    director,
    events,
    finops,
    films,
    flags,
    integrations,
    library,
    metrics,
    notifications,
    optim,
    prefs,
    recommendations,
    reports,
    search,
    sessions,
    translation,
    workspaces,
)
from app.billing import routes as billing  # additive: billing & payments domain

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
    notifications.router,  # notifications & webhooks platform (§5/§12)
    optim.router,
    # Realtime + API-quality layer (resumable SSE/WS, presence, cursor
    # pagination, versions). Additive — extends the §5.6 transport without
    # touching the round-1 event/session/director routes.
    realtime_router,
    analytics.router,  # product-analytics event pipeline (app/analytics/)
    assistant.router,  # reader assistant: grounded, spoiler-aware RAG Q&A (§8)
    finops.router,
    billing.router,  # billing & payments (additive)
    flags.router,  # feature flags & experimentation platform (app.flags)
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
