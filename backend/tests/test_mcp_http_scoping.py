"""Per-client scoping over the streamable-HTTP MCP transport (§12).

The HTTP control surface carries one shared bearer (the door gate) *and* a
per-client token table: a read-only token is resolved to a read-only identity
and denied a write/render tool, a full token is admitted, an unknown token is
403'd. These run entirely at the ASGI layer (the composition root opens no
sockets at build time) with a **fake** ``MemoryTools``.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.mcp.authz import MCPAuthorizationError
from app.mcp.identity import (
    ClientIdentity,
    ContextScopedAuthorizer,
    IdentityMiddleware,
    ScopedAuthorizer,
    StaticIdentityResolver,
    current_identity,
)
from app.mcp.server import build_streamable_http_app
from tests.test_mcp_protocol import FakeTools

_TOKENS = {
    "tok_ro": {"subject": "judge", "scopes": ["read"]},
    "tok_full": {"subject": "crew", "scopes": ["read", "write", "render"]},
}


# --------------------------------------------------------------------------- #
# IdentityMiddleware at the ASGI layer
# --------------------------------------------------------------------------- #


async def _capture_app(scope: Any, receive: Any, send: Any) -> None:
    ident = current_identity()
    payload: dict[str, Any]
    if ident is not None:
        payload = {"subject": ident.subject, "scopes": sorted(s.value for s in ident.scopes)}
    else:
        payload = {"subject": None}
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def test_identity_middleware_sets_contextvar_from_bearer() -> None:
    resolver = StaticIdentityResolver.from_config(_TOKENS, allow_anonymous=False)
    app = IdentityMiddleware(_capture_app, resolver=resolver)
    transport = ASGITransport(app=cast("Any", app))
    async with AsyncClient(transport=transport, base_url="http://mcp") as http:
        resp = await http.get("/", headers={"Authorization": "Bearer tok_ro"})
        assert resp.status_code == 200
        assert resp.json() == {"subject": "judge", "scopes": ["read"]}


async def test_identity_middleware_403s_unknown_token() -> None:
    resolver = StaticIdentityResolver.from_config(_TOKENS, allow_anonymous=False)
    app = IdentityMiddleware(_capture_app, resolver=resolver)
    transport = ASGITransport(app=cast("Any", app))
    async with AsyncClient(transport=transport, base_url="http://mcp") as http:
        resp = await http.get("/", headers={"Authorization": "Bearer ghost"})
        assert resp.status_code == 403
        assert "forbidden" in resp.text


async def test_identity_middleware_clears_contextvar_after_request() -> None:
    resolver = StaticIdentityResolver.from_config(_TOKENS, allow_anonymous=False)
    app = IdentityMiddleware(_capture_app, resolver=resolver)
    transport = ASGITransport(app=cast("Any", app))
    async with AsyncClient(transport=transport, base_url="http://mcp") as http:
        await http.get("/", headers={"Authorization": "Bearer tok_full"})
    # Outside any request the contextvar is back to None.
    assert current_identity() is None


# --------------------------------------------------------------------------- #
# ContextScopedAuthorizer reads the request identity
# --------------------------------------------------------------------------- #


async def test_context_scoped_authorizer_denies_without_identity() -> None:
    authz = ContextScopedAuthorizer(ScopedAuthorizer())
    with pytest.raises(MCPAuthorizationError):
        await authz.authorize("canon.query", {"book_id": "b1"})


async def test_context_scoped_authorizer_uses_resolved_identity() -> None:
    import app.mcp.identity as identity_mod

    authz = ContextScopedAuthorizer(ScopedAuthorizer())
    token = identity_mod._CURRENT_IDENTITY.set(ClientIdentity.read_only("judge"))
    try:
        await authz.authorize("canon.query", {"book_id": "b1"})  # read allowed
        with pytest.raises(MCPAuthorizationError):
            await authz.authorize("canon.upsert_entity", {"book_id": "b1"})  # write denied
    finally:
        identity_mod._CURRENT_IDENTITY.reset(token)


# --------------------------------------------------------------------------- #
# Full streamable-HTTP app: bearer gate + identity + scoping
# --------------------------------------------------------------------------- #


def _http_app(*, door_token: str | None = None) -> Any:
    # The per-client token table is the auth boundary (a resolver 403s unknown
    # tokens). ``door_token`` optionally layers a coarse shared gate on top.
    settings = Settings(
        dashscope_api_key="test", app_env="local", mcp_auth_token=door_token
    )
    resolver = StaticIdentityResolver.from_config(_TOKENS, allow_anonymous=False)
    authz = ContextScopedAuthorizer(ScopedAuthorizer())
    return build_streamable_http_app(
        FakeTools(),
        settings=settings,
        authorizer=authz,
        identity_resolver=resolver,
    )


async def test_http_app_403s_unknown_client_token() -> None:
    app = _http_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://mcp") as http:
            # "ghost" is not in the client table -> identity resolution 403s
            # before the MCP layer is ever reached.
            resp = await http.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer ghost",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert resp.status_code == 403


async def test_http_app_admits_known_client_token() -> None:
    app = _http_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://mcp") as http:
            # A recognised token passes the identity gate (the MCP layer then
            # handles the JSON-RPC). Anything other than 403/401 proves the gate
            # admitted it.
            resp = await http.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer tok_ro",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}}},
            )
            assert resp.status_code not in (401, 403)


async def test_http_app_layers_door_token_over_identity() -> None:
    app = _http_app(door_token="door")
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://mcp") as http:
            # With a door token set, a request missing it is 401'd by the outer
            # BearerAuthMiddleware before identity resolution.
            resp = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert resp.status_code == 401


def test_http_app_refuses_no_auth_boundary_outside_local() -> None:
    settings = Settings(
        dashscope_api_key="test",
        app_env="production",
        jwt_secret="a-real-production-secret-32-bytes-x",
        mcp_auth_token=None,
    )
    with pytest.raises(RuntimeError, match="auth boundary"):
        build_streamable_http_app(FakeTools(), settings=settings)


def test_http_app_identity_resolver_satisfies_non_local_auth() -> None:
    # A per-client token table is a valid auth boundary outside local.
    settings = Settings(
        dashscope_api_key="test",
        app_env="production",
        jwt_secret="a-real-production-secret-32-bytes-x",
        mcp_auth_token=None,
    )
    resolver = StaticIdentityResolver.from_config(_TOKENS, allow_anonymous=False)
    app = build_streamable_http_app(
        FakeTools(), settings=settings, identity_resolver=resolver
    )
    assert app is not None
