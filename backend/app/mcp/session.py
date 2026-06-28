"""Per-client MCP session state (identity + negotiated caps + subscriptions).

The streamable-HTTP MCP can be stateless (each request is self-contained) or
session-oriented (the client initializes once, then issues many calls under a
session id). Kinora's resource subscriptions are inherently stateful — a client
subscribes once and is notified on later writes — so this module models the
session the subscription + scoping layers need, independent of the transport.

A :class:`ClientSession` bundles:

* the resolved :class:`~app.mcp.identity.ClientIdentity` (who is calling),
* the :class:`~app.mcp.capabilities.NegotiatedCapabilities` agreed at
  ``initialize`` (what they may use),
* the set of resource URIs they subscribe to (delegated to the shared
  :class:`~app.mcp.resources.SubscriptionRegistry`).

:class:`SessionStore` is the in-memory registry of live sessions, keyed by the
SDK's session id. It is per-process (the canon MCP runs in one process) and is
where the server resolves the identity for an incoming call and where it drops a
client's subscriptions on disconnect.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from app.mcp.capabilities import NegotiatedCapabilities
from app.mcp.identity import ClientIdentity
from app.mcp.registry import Scope
from app.mcp.resources import SubscriptionRegistry


@dataclass(slots=True)
class ClientSession:
    """One live MCP client's state for the lifetime of its connection."""

    session_id: str
    identity: ClientIdentity
    capabilities: NegotiatedCapabilities
    subscriptions: SubscriptionRegistry
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Record activity (for idle reaping)."""
        self.last_seen = time.monotonic()

    def subscribe(self, uri: str) -> None:
        """Subscribe this client to a resource URI."""
        self.subscriptions.subscribe(self.session_id, uri)

    def unsubscribe(self, uri: str) -> None:
        """Unsubscribe this client from a resource URI."""
        self.subscriptions.unsubscribe(self.session_id, uri)

    def subscribed_uris(self) -> set[str]:
        """The resource URIs this client currently watches."""
        return self.subscriptions.subscriptions_for(self.session_id)

    def may(self, scope: Scope) -> bool:
        """True when this session is permitted to use ``scope`` (identity ∩ negotiated)."""
        return self.identity.allows_scope(scope) and self.capabilities.allows(scope)

    def close(self) -> None:
        """Drop all of this client's subscriptions on disconnect."""
        self.subscriptions.drop_client(self.session_id)


class SessionStore:
    """In-memory registry of live MCP sessions (per-process).

    Sessions share one :class:`SubscriptionRegistry` so a write's fan-out reaches
    every watcher regardless of which session opened it. Idle sessions can be
    reaped via :meth:`reap_idle`.
    """

    def __init__(self, *, subscriptions: SubscriptionRegistry | None = None) -> None:
        self._subscriptions = subscriptions or SubscriptionRegistry()
        self._sessions: dict[str, ClientSession] = {}

    @property
    def subscriptions(self) -> SubscriptionRegistry:
        """The shared subscription registry (the server's notification source)."""
        return self._subscriptions

    def open(
        self,
        identity: ClientIdentity,
        capabilities: NegotiatedCapabilities,
        *,
        session_id: str | None = None,
    ) -> ClientSession:
        """Create and register a new session (generates an id when absent)."""
        sid = session_id or secrets.token_urlsafe(16)
        session = ClientSession(
            session_id=sid,
            identity=identity,
            capabilities=capabilities,
            subscriptions=self._subscriptions,
        )
        self._sessions[sid] = session
        return session

    def get(self, session_id: str) -> ClientSession | None:
        """The live session for ``session_id`` (``None`` when unknown)."""
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> None:
        """Close + drop a session and its subscriptions (idempotent)."""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def reap_idle(self, *, max_idle_s: float) -> int:
        """Close sessions idle longer than ``max_idle_s``; return the count reaped."""
        now = time.monotonic()
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_seen > max_idle_s
        ]
        for sid in stale:
            self.close(sid)
        return len(stale)

    @property
    def count(self) -> int:
        return len(self._sessions)


__all__ = ["ClientSession", "SessionStore"]
