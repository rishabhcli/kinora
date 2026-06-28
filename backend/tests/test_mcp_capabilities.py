"""Unit tests for MCP capability advertisement + negotiation (§8.3).

The server advertises a stable capability descriptor (tools/resources +
Kinora experimental versioning/scopes) and negotiates the session-usable subset
against a client's declared capabilities. No infrastructure required.
"""

from __future__ import annotations

from app.mcp.capabilities import (
    KINORA_EXPERIMENTAL_KEY,
    PROTOCOL_VERSION,
    ClientCapabilities,
    ServerCapabilities,
    negotiate,
)
from app.mcp.registry import Scope


def test_server_advertises_resources_and_experimental() -> None:
    caps = ServerCapabilities.for_catalog().to_dict()
    assert caps["resources"]["subscribe"] is True
    assert caps["resources"]["listChanged"] is True
    assert caps["tools"]["listChanged"] is True
    ext = caps["experimental"][KINORA_EXPERIMENTAL_KEY]
    assert ext["versioning"] is True
    assert set(ext["scopes"]) == {"read", "write", "render"}
    assert ext["structuredErrors"] is True


def test_initialize_result_shape() -> None:
    init = ServerCapabilities.for_catalog().initialize_result()
    assert init["protocolVersion"] == PROTOCOL_VERSION
    assert init["serverInfo"]["name"] == "kinora-canon-memory"
    assert "capabilities" in init


def test_client_capabilities_parse_is_lenient() -> None:
    c = ClientCapabilities.from_dict(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "resources": {"subscribe": True, "listChanged": True},
                "experimental": {KINORA_EXPERIMENTAL_KEY: {"versioning": True, "scopes": ["read"]}},
            },
        }
    )
    assert c.wants_resource_subscriptions
    assert c.wants_list_changed
    assert c.understands_versioning
    assert c.requested_scopes == ("read",)


def test_client_capabilities_empty_defaults() -> None:
    c = ClientCapabilities.from_dict(None)
    assert not c.wants_resource_subscriptions
    assert not c.understands_versioning
    assert c.requested_scopes == ()


def test_negotiate_intersects_features() -> None:
    server = ServerCapabilities.for_catalog()
    client = ClientCapabilities(
        wants_resource_subscriptions=True,
        wants_list_changed=False,
        understands_versioning=True,
        requested_scopes=("read",),
    )
    n = negotiate(server, client, allowed_scopes=frozenset({Scope.READ, Scope.WRITE}))
    assert n.resource_subscriptions is True  # both sides agree
    assert n.list_changed is False  # client didn't ask
    assert n.versioning is True
    assert n.granted_scopes == frozenset({Scope.READ})  # allowed ∩ requested


def test_negotiate_caps_to_allowed_scopes() -> None:
    server = ServerCapabilities.for_catalog()
    # Client asks for write but is only allowed read.
    client = ClientCapabilities(requested_scopes=("read", "write"))
    n = negotiate(server, client, allowed_scopes=frozenset({Scope.READ}))
    assert n.granted_scopes == frozenset({Scope.READ})
    assert not n.allows(Scope.WRITE)


def test_negotiate_default_grant_when_client_silent_on_scopes() -> None:
    server = ServerCapabilities.for_catalog()
    client = ClientCapabilities()  # speaks no scopes
    n = negotiate(server, client, allowed_scopes=frozenset({Scope.READ, Scope.RENDER}))
    # Falls back to the identity's full allowance.
    assert n.granted_scopes == frozenset({Scope.READ, Scope.RENDER})


def test_negotiate_ignores_unknown_scope_names() -> None:
    server = ServerCapabilities.for_catalog()
    client = ClientCapabilities(requested_scopes=("read", "bogus"))
    n = negotiate(server, client)
    assert n.granted_scopes == frozenset({Scope.READ})
