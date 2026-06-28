"""Realtime services bundle + app-state accessors (kinora.md §5.6).

The realtime layer needs four Redis-backed services — the event log, the
connection registry, presence, and the idempotency store — each constructed from
the container's ``redis`` + ``settings``. Rather than edit the shared
:mod:`app.composition` ``Container`` (owned additively by every agent), this
module bundles them in a :class:`RealtimeServices` object cached on
``app.state.realtime`` and built lazily on first access.

The bundle is the single seam routes and the sweeper resolve through, so tests
can inject a bundle backed by throwaway infra (or fakes) by setting
``app.state.realtime`` before the app starts, exactly mirroring how
``app.state.container`` is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request, WebSocket

from app.api.realtime.connections import ConnectionRegistry
from app.api.realtime.event_log import EventLog
from app.api.realtime.idempotency import IdempotencyStore
from app.api.realtime.presence import PresenceService

if TYPE_CHECKING:
    from app.composition import Container


@dataclass(slots=True)
class RealtimeServices:
    """The four Redis-backed realtime services, built from one container."""

    event_log: EventLog
    connections: ConnectionRegistry
    presence: PresenceService
    idempotency: IdempotencyStore

    @classmethod
    def from_container(cls, container: Container) -> RealtimeServices:
        """Construct the bundle from a wired container's Redis + settings."""
        redis = container.redis
        return cls(
            event_log=EventLog(redis),
            connections=ConnectionRegistry(redis),
            presence=PresenceService(redis),
            idempotency=IdempotencyStore(redis),
        )


def get_realtime(request: Request) -> RealtimeServices:
    """Resolve (and lazily build + cache) the realtime bundle off ``app.state``."""
    return _resolve(request.app)


def get_realtime_ws(websocket: WebSocket) -> RealtimeServices:
    """The WebSocket-scoped twin of :func:`get_realtime`."""
    return _resolve(websocket.app)


def _resolve(app: object) -> RealtimeServices:
    from app.api.errors import APIError

    state = getattr(app, "state", None)
    existing = getattr(state, "realtime", None)
    if isinstance(existing, RealtimeServices):
        return existing
    container = getattr(state, "container", None)
    if container is None:  # pragma: no cover - guards a misconfigured app
        raise APIError("internal_error", "application container is not initialized", status=500)
    bundle = RealtimeServices.from_container(container)
    if state is not None:
        state.realtime = bundle
    return bundle


__all__ = ["RealtimeServices", "get_realtime", "get_realtime_ws"]
