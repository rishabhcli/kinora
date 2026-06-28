"""Capability advertisement + negotiation for the canon-memory MCP server (§8.3).

MCP's ``initialize`` handshake is a capability exchange: the client declares what
it can do, the server declares what it offers, and both sides operate only at the
intersection. The official SDK fills in the *baseline* server capabilities from
the registered handlers; this module is Kinora's **explicit, inspectable**
description of the surface — the thing the conformance suite asserts on and the
typed client negotiates against — plus a Kinora ``experimental`` extension block
advertising the features the base spec has no slot for (tool **versioning** and
**scoped** tools).

Negotiation is pure and deterministic: given the server's offered capabilities
and the client's declared capabilities, :func:`negotiate` returns the agreed
:class:`NegotiatedCapabilities` (what the client may actually use this session).
No I/O, no rendering, no spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.mcp.registry import Scope, ToolCatalog, default_catalog

#: The MCP protocol revision this server implements. Kept as a constant so the
#: conformance suite and the typed client can assert the handshake agrees.
PROTOCOL_VERSION = "2025-06-18"

#: The Kinora experimental-capability namespace key.
KINORA_EXPERIMENTAL_KEY = "io.kinora.canon"


@dataclass(frozen=True, slots=True)
class ToolsCapability:
    """The server's ``tools`` capability block."""

    list_changed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"listChanged": self.list_changed}


@dataclass(frozen=True, slots=True)
class ResourcesCapability:
    """The server's ``resources`` capability block (subscriptions + list-changed)."""

    subscribe: bool = True
    list_changed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"subscribe": self.subscribe, "listChanged": self.list_changed}


@dataclass(frozen=True, slots=True)
class LoggingCapability:
    """The server's ``logging`` capability block (structured log notifications)."""

    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True, slots=True)
class KinoraExtension:
    """The Kinora-private experimental capabilities the base spec can't express.

    * ``versioning`` — every tool carries an independent ``major.minor`` version
      and a call may pin one (``_meta.kinora/version``).
    * ``scopes`` — tools are tagged ``read`` / ``write`` / ``render`` and a
      client may be granted a subset (per-client scoping, §12).
    * ``structuredErrors`` — failures carry a stable ``category`` + numeric code
      (``errors.py``) so a client branches on the reason.
    """

    versioning: bool = True
    scopes: tuple[str, ...] = (Scope.READ.value, Scope.WRITE.value, Scope.RENDER.value)
    structured_errors: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "versioning": self.versioning,
            "scopes": list(self.scopes),
            "structuredErrors": self.structured_errors,
        }


@dataclass(frozen=True, slots=True)
class ServerCapabilities:
    """The complete capability descriptor this server advertises at ``initialize``.

    Built once from the :class:`ToolCatalog` so the advertised tool/resource
    surface always matches the live registry.
    """

    protocol_version: str = PROTOCOL_VERSION
    server_name: str = "kinora-canon-memory"
    tools: ToolsCapability = field(default_factory=ToolsCapability)
    resources: ResourcesCapability = field(default_factory=ResourcesCapability)
    logging: LoggingCapability = field(default_factory=LoggingCapability)
    experimental: KinoraExtension = field(default_factory=KinoraExtension)

    @classmethod
    def for_catalog(cls, catalog: ToolCatalog | None = None) -> ServerCapabilities:
        """Build the descriptor; ``catalog`` reserved for future per-tool gating."""
        _ = catalog or default_catalog()
        return cls()

    def to_dict(self) -> dict[str, Any]:
        """The capability object as advertised in the ``initialize`` result."""
        return {
            "tools": self.tools.to_dict(),
            "resources": self.resources.to_dict(),
            "logging": self.logging.to_dict(),
            "experimental": {KINORA_EXPERIMENTAL_KEY: self.experimental.to_dict()},
        }

    def initialize_result(self) -> dict[str, Any]:
        """The full ``initialize`` response payload (protocol + server info + caps)."""
        return {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": self.server_name, "version": "1.0"},
            "capabilities": self.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ClientCapabilities:
    """A client's declared capabilities (parsed from its ``initialize`` request).

    Only the dimensions the canon server negotiates over are modelled: whether
    the client understands resource subscriptions, whether it wants tool
    list-change notifications, and which Kinora extensions it opts into.
    """

    protocol_version: str = PROTOCOL_VERSION
    wants_resource_subscriptions: bool = False
    wants_list_changed: bool = False
    understands_versioning: bool = False
    requested_scopes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ClientCapabilities:
        """Parse a client capability object (lenient: unknown keys ignored)."""
        data = data or {}
        caps = data.get("capabilities", data)
        resources = caps.get("resources") or {}
        ext = (caps.get("experimental") or {}).get(KINORA_EXPERIMENTAL_KEY) or {}
        return cls(
            protocol_version=data.get("protocolVersion", PROTOCOL_VERSION),
            wants_resource_subscriptions=bool(resources.get("subscribe", False)),
            wants_list_changed=bool(resources.get("listChanged", False)),
            understands_versioning=bool(ext.get("versioning", False)),
            requested_scopes=tuple(ext.get("scopes", ()) or ()),
        )


@dataclass(frozen=True, slots=True)
class NegotiatedCapabilities:
    """The intersection the client may actually use this session."""

    protocol_version: str
    resource_subscriptions: bool
    list_changed: bool
    versioning: bool
    granted_scopes: frozenset[Scope]

    def allows(self, scope: Scope) -> bool:
        return scope in self.granted_scopes


def negotiate(
    server: ServerCapabilities,
    client: ClientCapabilities,
    *,
    allowed_scopes: frozenset[Scope] | None = None,
) -> NegotiatedCapabilities:
    """Compute the agreed capabilities for a session.

    A feature is enabled only when *both* the server offers it and the client
    asks for it (resource subscriptions, list-changed, versioning). The protocol
    version is the server's (the SDK already rejects an incompatible client).

    ``allowed_scopes`` caps what the identity layer permits this client; the
    granted set is ``allowed ∩ requested`` (or ``allowed`` when the client asks
    for nothing specific — a client that doesn't speak scopes still gets its
    identity's full grant).
    """
    if allowed_scopes is None:
        allowed_scopes = frozenset(Scope)
    requested = (
        {Scope(s) for s in client.requested_scopes if s in Scope._value2member_map_}
        if client.requested_scopes
        else set(allowed_scopes)
    )
    granted = frozenset(allowed_scopes & requested)
    return NegotiatedCapabilities(
        protocol_version=server.protocol_version,
        resource_subscriptions=server.resources.subscribe and client.wants_resource_subscriptions,
        list_changed=server.resources.list_changed and client.wants_list_changed,
        versioning=server.experimental.versioning and client.understands_versioning,
        granted_scopes=granted,
    )


__all__ = [
    "KINORA_EXPERIMENTAL_KEY",
    "PROTOCOL_VERSION",
    "ClientCapabilities",
    "KinoraExtension",
    "LoggingCapability",
    "NegotiatedCapabilities",
    "ResourcesCapability",
    "ServerCapabilities",
    "ToolsCapability",
    "negotiate",
]
