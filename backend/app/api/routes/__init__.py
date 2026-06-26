"""API route modules, mounted under ``/api`` by :func:`app.main.create_app`."""

from __future__ import annotations

from app.api.routes import auth, books, director, events, metrics, prefs, sessions

#: The routers mounted (in order) under the versioned ``/api`` prefix.
# NOTE (Agent 07): the new ``optim`` router (cost/perf) is intentionally NOT wired here —
# router registration is a shared seam owned by Agent 12 (see coordination/requests/agent-07.md R2).
# The router + its tests are complete; wiring is a one-liner: add ``optim`` to the import and
# ``optim.router`` to this list.
ROUTERS = [
    auth.router,
    books.router,
    sessions.router,
    director.router,
    prefs.router,
    events.router,
    metrics.router,
]

__all__ = ["ROUTERS"]
