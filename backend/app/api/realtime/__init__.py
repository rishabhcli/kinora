"""Production realtime + API-quality layer for the gateway (kinora.md §5.1–§5.6).

This package sits *additively* on top of round-1's :mod:`app.api.routes.events`
SSE/WS forwarder. It adds the pieces a long-lived, multi-reader transport needs:

* :mod:`app.api.realtime.event_log` — a per-session append-only event log so a
  dropped SSE/WS connection can **resume** from a ``Last-Event-ID`` instead of
  losing every event in the gap (§5.6).
* :mod:`app.api.realtime.sse` — SSE framing (``id:``/``event:``/``data:``/
  ``retry:``), heartbeats, and a reusable resumable :class:`EventStream`.
* :mod:`app.api.realtime.connections` — a Redis-backed **connection registry**
  (per-session/per-user counts, caps, idle reaping) that works across replicas.
* :mod:`app.api.realtime.presence` — **multiplayer presence**: who is co-reading
  a session, their cursor + mode, with TTL'd heartbeats and join/leave fan-out.
* :mod:`app.api.realtime.idempotency` — **idempotency keys** for unsafe POSTs.
* :mod:`app.api.realtime.pagination` — opaque **cursor pagination**.
* :mod:`app.api.realtime.ratelimit` — a composable per-route/per-user limiter
  that emits the standard ``RateLimit-*`` / ``Retry-After`` headers.
* :mod:`app.api.realtime.versioning` — **API versioning + deprecation** signals.
* :mod:`app.api.realtime.routes_realtime` — the thin routers that expose all of
  the above, mounted under ``/api`` next to the round-1 routers.

Nothing here spends model credits: the layer only moves already-produced §5.6
events around and keeps connection bookkeeping. ``KINORA_LIVE_VIDEO`` is
irrelevant to it.
"""

from __future__ import annotations

__all__: list[str] = []
