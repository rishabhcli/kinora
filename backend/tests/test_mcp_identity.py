"""Unit tests for MCP per-client identity + scope/book authorization (§12).

The scope layer answers *who is calling and what may they do*: a read-only token
cannot invoke a write/render tool, and a token confined to a book allowlist is
denied off-allowlist calls. Identity is resolved from a configured token table
and (in local) an anonymous fallback. No infrastructure required.
"""

from __future__ import annotations

import pytest

from app.mcp.authz import MCPAuthorizationError
from app.mcp.identity import (
    ClientIdentity,
    ScopedAuthorizer,
    StaticIdentityResolver,
)
from app.mcp.registry import Scope

# --- identity resolution -----------------------------------------------------


async def test_static_resolver_maps_tokens_to_grants() -> None:
    res = StaticIdentityResolver.from_config(
        {
            "tok_ro": {"subject": "judge", "scopes": ["read"]},
            "tok_full": {"scopes": ["read", "write", "render"], "books": ["book_1"]},
        },
        allow_anonymous=False,
    )
    ro = await res.resolve("tok_ro")
    assert ro.subject == "judge"
    assert ro.scopes == frozenset({Scope.READ})
    full = await res.resolve("Bearer tok_full")  # strips the Bearer prefix
    assert full.scopes == frozenset({Scope.READ, Scope.WRITE, Scope.RENDER})
    assert full.book_allowlist == frozenset({"book_1"})


async def test_static_resolver_denies_unknown_token_when_table_set() -> None:
    res = StaticIdentityResolver.from_config({"tok": {"scopes": ["read"]}}, allow_anonymous=False)
    with pytest.raises(MCPAuthorizationError):
        await res.resolve("ghost")


async def test_static_resolver_anonymous_fallback_when_no_table() -> None:
    res = StaticIdentityResolver.from_config(None, allow_anonymous=True)
    anon = await res.resolve(None)
    assert anon.scopes == frozenset(Scope)  # full open local surface


async def test_grant_defaults_to_read_only() -> None:
    res = StaticIdentityResolver.from_config({"tok": {}}, allow_anonymous=False)
    ident = await res.resolve("tok")
    assert ident.scopes == frozenset({Scope.READ})


# --- scope enforcement -------------------------------------------------------


async def test_read_only_client_denied_writes_and_renders() -> None:
    auth = ScopedAuthorizer()
    ro = ClientIdentity.read_only("judge")
    await auth.authorize_as(ro, "canon.query", {"book_id": "b1"})  # allowed
    with pytest.raises(MCPAuthorizationError):
        await auth.authorize_as(ro, "canon.upsert_entity", {"book_id": "b1"})
    with pytest.raises(MCPAuthorizationError):
        await auth.authorize_as(ro, "shot.render", {"book_id": "b1"})


async def test_full_client_confined_to_book_allowlist() -> None:
    auth = ScopedAuthorizer()
    full = ClientIdentity(
        subject="crew",
        scopes=frozenset({Scope.READ, Scope.WRITE, Scope.RENDER}),
        book_allowlist=frozenset({"book_1"}),
    )
    await auth.authorize_as(full, "shot.render", {"book_id": "book_1"})
    with pytest.raises(MCPAuthorizationError):
        await auth.authorize_as(full, "canon.query", {"book_id": "book_2"})


async def test_empty_allowlist_means_any_book() -> None:
    auth = ScopedAuthorizer()
    full = ClientIdentity.full("crew")
    await auth.authorize_as(full, "canon.query", {"book_id": "anything"})


async def test_unknown_tool_denied() -> None:
    auth = ScopedAuthorizer()
    with pytest.raises(MCPAuthorizationError):
        await auth.authorize_as(ClientIdentity.full(), "nope.nope", {})


async def test_chains_next_authorizer_after_scope_check() -> None:
    seen: list[tuple[str, object]] = []

    async def nxt(tool: str, args: dict[str, object]) -> None:
        seen.append((tool, args.get("book_id")))

    auth = ScopedAuthorizer(next_authorizer=nxt)
    bound = auth.for_identity(ClientIdentity.full())
    await bound.authorize("canon.query", {"book_id": "b1"})
    assert seen == [("canon.query", "b1")]


async def test_next_authorizer_not_reached_when_scope_denied() -> None:
    called = False

    async def nxt(tool: str, args: dict[str, object]) -> None:
        nonlocal called
        called = True

    auth = ScopedAuthorizer(next_authorizer=nxt)
    ro = ClientIdentity.read_only("judge")
    with pytest.raises(MCPAuthorizationError):
        await auth.authorize_as(ro, "canon.upsert_entity", {"book_id": "b1"})
    assert called is False  # the scope check short-circuits the chain
