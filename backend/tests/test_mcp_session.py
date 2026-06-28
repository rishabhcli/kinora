"""Unit tests for the MCP per-client session store + idle reaping.

A session bundles a resolved identity + negotiated capabilities + subscriptions;
the store registers live sessions, fans subscriptions through a shared registry,
and reaps idle ones. No infrastructure required.
"""

from __future__ import annotations

import time

from app.mcp.capabilities import NegotiatedCapabilities
from app.mcp.identity import ClientIdentity
from app.mcp.registry import Scope
from app.mcp.session import SessionStore


def _caps(*scopes: Scope) -> NegotiatedCapabilities:
    return NegotiatedCapabilities(
        protocol_version="2025-06-18",
        resource_subscriptions=True,
        list_changed=True,
        versioning=True,
        granted_scopes=frozenset(scopes),
    )


def test_open_and_get_session() -> None:
    store = SessionStore()
    s = store.open(ClientIdentity.read_only("judge"), _caps(Scope.READ))
    assert store.get(s.session_id) is s
    assert store.count == 1


def test_subscriptions_shared_across_sessions() -> None:
    store = SessionStore()
    a = store.open(ClientIdentity.full("a"), _caps(Scope.READ))
    b = store.open(ClientIdentity.full("b"), _caps(Scope.READ))
    a.subscribe("kinora://canon/book_1")
    b.subscribe("kinora://canon/book_1")
    # Both watch the same resource through the shared registry.
    fan = store.subscriptions.fan_out(["kinora://canon/book_1"])
    assert set(fan) == {a.session_id, b.session_id}


def test_close_drops_subscriptions() -> None:
    store = SessionStore()
    s = store.open(ClientIdentity.full("a"), _caps(Scope.READ))
    s.subscribe("kinora://canon/book_1")
    store.close(s.session_id)
    assert store.get(s.session_id) is None
    assert store.subscriptions.total_subscriptions == 0


def test_session_may_intersects_identity_and_capabilities() -> None:
    store = SessionStore()
    # Identity grants read+write but the negotiated caps only granted read.
    ident = ClientIdentity(subject="x", scopes=frozenset({Scope.READ, Scope.WRITE}))
    s = store.open(ident, _caps(Scope.READ))
    assert s.may(Scope.READ)
    assert not s.may(Scope.WRITE)  # capped by negotiated capabilities


def test_reap_idle() -> None:
    store = SessionStore()
    s = store.open(ClientIdentity.full("a"), _caps(Scope.READ))
    # Force the session to look old.
    s.last_seen = time.monotonic() - 1000
    reaped = store.reap_idle(max_idle_s=10.0)
    assert reaped == 1
    assert store.count == 0


def test_explicit_session_id_is_honoured() -> None:
    store = SessionStore()
    s = store.open(ClientIdentity.full("a"), _caps(Scope.READ), session_id="sid-123")
    assert s.session_id == "sid-123"
    assert store.get("sid-123") is s
