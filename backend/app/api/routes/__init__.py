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
    films,
    finops,
    flags,
    integrations,
    library,
    llmops,
    media,
    metrics,
    notifications,
    optim,
    portability,
    prefs,
    recommendations,
    reports,
    search,
    sessions,
    translation,
    usage_analytics,
    workspaces,
)
from app.billing import routes as billing  # additive: billing & payments domain
from app.compliance.api import router as compliance_router  # additive: compliance domain

# Additive: the unified runtime feature-flag / dynamic-config plane
# (app.flags.plane). The operational config overlay over Settings, with guarded
# kill-switches (KINORA_LIVE_VIDEO can only be forced OFF), targeting, sticky
# rollouts, audit, and hot-reload. Distinct from the /flags experimentation API.
from app.flags.plane.api import router as runtime_config_router

# Additive: the content-moderation & safety admin/operations surface (§9/§10).
# The router lives under app.moderation to keep the safety domain self-contained.
from app.moderation.routes import router as moderation_router

# Additive: the sandboxed plugin/extension platform (app.platform.plugins).
# Self-contained marketplace + lifecycle + dispatch surface under /plugins.
from app.platform.plugins.api import router as plugins_router

# Additive: deep health + SLO / error-budget tracking (app.slo). Self-contained
# reliability plane under /slo (deep readiness, SLI/budget status, burn alerts,
# release gate). Distinct from — and never touches — the round-1 root /health + /ready.
from app.slo.api import router as slo_router

# Additive: the read-only video-provider marketplace catalog (app.video.marketplace).
# Self-contained, in-memory, seeded; browse/search/compare/migration under
# /video/marketplace. No DB / network / container dependency.
from app.video.marketplace.api import router as video_marketplace_router

# Additive: read-only video-provider registry introspection (app.video.registry).
# Catalog/capabilities/canary view under /video; never renders or spends.
from app.video.registry.api import router as video_registry_router

# Additive: the async-video/audio provider webhook ingress gateway
# (app.video.webhooks). Verifies signatures, normalises callbacks, dedups
# at-least-once deliveries, and fast-ACKs to a local JobCompletionSink (§12.1).
from app.video.webhooks import router as video_webhooks_router

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
    usage_analytics.router,  # cost & usage analytics + dashboards (app/usageanalytics/)
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
    compliance_router,  # /compliance: consent, retention, DSAR, holds, ledger, report
    llmops.router,  # LLM-ops surface (prompt registry/eval/guardrails); 404 unless llmops_enabled
    portability.router,  # data export/import & portability (book/canon/account/backup)
    media.router,  # Media domain: /api/media asset registry (additive)
    plugins_router,  # sandboxed plugin/extension platform (app.platform.plugins)
    runtime_config_router,  # unified runtime config plane (app.flags.plane) at /runtime-config
    video_registry_router,  # read-only video-provider registry introspection (app.video.registry)
    slo_router,  # deep health + SLO / error-budget / release-gate plane (app.slo)
    video_marketplace_router,  # read-only video-provider marketplace (app.video.marketplace)
    video_webhooks_router,  # async-video provider webhook ingress (app.video.webhooks, §12.1)
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
