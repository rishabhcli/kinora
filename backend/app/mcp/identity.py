"""Caller identity + per-client scoping for the MCP control surface (§12).

``server.py``'s :class:`BearerAuthMiddleware` answers *is this request allowed
through the door* (one shared token). ``authz.py``'s :class:`BookScopedAuthorizer`
answers *does this book_id exist*. Neither answers **who is calling and what may
they do** — and the design's standing follow-up (server.py docstring, §12) is to
thread a real caller identity from the bearer subject into a per-client check.

This module supplies that layer:

* :class:`ClientIdentity` — the resolved caller (a subject id + a granted scope
  set + an optional book allowlist), derived from a bearer token.
* :class:`IdentityResolver` — maps a bearer token to a :class:`ClientIdentity`.
  The default :class:`StaticIdentityResolver` reads a token→scope table from
  configuration, so deployments can issue a read-only token and a full token
  without code changes. An anonymous fallback (used in ``local`` when no table
  is configured) grants every scope to preserve the open local surface.
* :class:`ScopedAuthorizer` — an :class:`~app.mcp.server.ToolAuthorizer` that
  enforces *both* dimensions: the tool's required scope must be in the
  identity's grant, and (when the identity has a book allowlist) the call's
  ``book_id`` must be in it. It composes with the existing book-existence
  authorizer so the two checks stack rather than replace.

Pure policy — no rendering, no spend.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.mcp.authz import MCPAuthorizationError
from app.mcp.registry import Scope, ToolCatalog, default_catalog

#: A composed authorizer this layer can chain after the scope check.
NextAuthorizer = Callable[[str, dict[str, Any]], Awaitable[None]]

#: The per-request resolved identity, set by :class:`IdentityMiddleware` and read
#: by :class:`ContextScopedAuthorizer`. A contextvar (not a global) so concurrent
#: requests on the ASGI server each see their own caller.
_CURRENT_IDENTITY: contextvars.ContextVar[ClientIdentity | None] = contextvars.ContextVar(
    "kinora_mcp_identity", default=None
)


def current_identity() -> ClientIdentity | None:
    """The identity resolved for the in-flight request (``None`` outside one)."""
    return _CURRENT_IDENTITY.get()


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """A resolved MCP caller: who they are and what they may do.

    ``scopes`` is the set of capabilities granted (read / write / render).
    ``book_allowlist`` — when non-empty — restricts the caller to those books;
    empty means *any book* (subject still subject to the book-existence check).
    """

    subject: str
    scopes: frozenset[Scope]
    book_allowlist: frozenset[str] = field(default_factory=frozenset)
    label: str = ""

    def allows_scope(self, scope: Scope) -> bool:
        return scope in self.scopes

    def allows_book(self, book_id: str | None) -> bool:
        if not self.book_allowlist:
            return True
        if book_id is None:
            return True
        return book_id in self.book_allowlist

    @classmethod
    def full(cls, subject: str = "anonymous", *, label: str = "open") -> ClientIdentity:
        """An identity granted every scope and every book (the open local surface)."""
        return cls(subject=subject, scopes=frozenset(Scope), label=label)

    @classmethod
    def read_only(cls, subject: str, **kw: Any) -> ClientIdentity:
        """A convenience constructor for a read-only client."""
        return cls(subject=subject, scopes=frozenset({Scope.READ}), **kw)


class IdentityResolver:
    """Resolve a bearer token to a :class:`ClientIdentity` (protocol)."""

    async def resolve(self, token: str | None) -> ClientIdentity:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TokenGrant:
    """A configured token's grant: its scopes + optional book allowlist."""

    subject: str
    scopes: frozenset[Scope]
    book_allowlist: frozenset[str] = field(default_factory=frozenset)
    label: str = ""

    @classmethod
    def from_config(cls, subject: str, raw: dict[str, Any]) -> TokenGrant:
        """Parse one entry of the token→grant config map.

        Shape: ``{"scopes": ["read","write"], "books": ["book_1"], "label": "…"}``.
        An absent / empty ``scopes`` defaults to read-only (fail safe).
        """
        scope_names = raw.get("scopes") or ["read"]
        scopes = frozenset(
            Scope(s) for s in scope_names if s in Scope._value2member_map_
        ) or frozenset({Scope.READ})
        books = frozenset(str(b) for b in (raw.get("books") or ()))
        return cls(
            subject=subject,
            scopes=scopes,
            book_allowlist=books,
            label=str(raw.get("label", subject)),
        )

    def to_identity(self) -> ClientIdentity:
        return ClientIdentity(
            subject=self.subject,
            scopes=self.scopes,
            book_allowlist=self.book_allowlist,
            label=self.label,
        )


class StaticIdentityResolver(IdentityResolver):
    """Resolve identities from a fixed token→grant table.

    Built from configuration (``Settings.mcp_client_scopes``). When the table is
    empty *and* an anonymous fallback is allowed, an unknown / absent token maps
    to the full open identity (the local-dev surface). When the table is
    non-empty, an unrecognised token is denied — issuing scoped tokens
    intentionally locks the surface down.
    """

    def __init__(
        self,
        grants: dict[str, TokenGrant],
        *,
        anonymous: ClientIdentity | None = None,
    ) -> None:
        self._grants = dict(grants)
        self._anonymous = anonymous

    @classmethod
    def from_config(
        cls,
        token_table: dict[str, dict[str, Any]] | None,
        *,
        allow_anonymous: bool,
    ) -> StaticIdentityResolver:
        """Build from the raw config map; ``allow_anonymous`` gates the open fallback."""
        grants: dict[str, TokenGrant] = {}
        for token, raw in (token_table or {}).items():
            subject = str(raw.get("subject", token[:8]))
            grants[token] = TokenGrant.from_config(subject, raw)
        anonymous = ClientIdentity.full() if allow_anonymous else None
        return cls(grants, anonymous=anonymous)

    async def resolve(self, token: str | None) -> ClientIdentity:
        if token is not None:
            token = token.removeprefix("Bearer ").strip()
        if token and token in self._grants:
            return self._grants[token].to_identity()
        if not self._grants and self._anonymous is not None:
            # No table configured at all -> open local surface.
            return self._anonymous
        if self._anonymous is not None and not token:
            return self._anonymous
        raise MCPAuthorizationError("unrecognised MCP client token")


class ScopedAuthorizer:
    """An MCP authorizer enforcing per-client scope + book allowlist (§12).

    Implements the :class:`app.mcp.server.ToolAuthorizer` protocol. For each
    call it looks up the tool's required scope in the catalog, asserts the
    identity grants it and (when present) the book allowlist permits the call's
    ``book_id``, then chains an optional ``next_authorizer`` (the existing
    book-existence check). The identity is supplied per-call via
    :meth:`for_identity`, which returns a bound authorizer the session uses.
    """

    def __init__(
        self,
        *,
        catalog: ToolCatalog | None = None,
        next_authorizer: NextAuthorizer | None = None,
    ) -> None:
        self._catalog = catalog or default_catalog()
        self._next = next_authorizer

    def required_scopes(self, tool_name: str) -> frozenset[Scope]:
        """The scope(s) a caller must hold to invoke ``tool_name``."""
        meta = self._catalog.get(tool_name)
        if meta is None:
            return frozenset({Scope.READ})
        return meta.scopes

    async def authorize_as(
        self, identity: ClientIdentity, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        """Authorize ``tool_name`` for ``identity`` (raises to deny)."""
        meta = self._catalog.get(tool_name)
        if meta is None:
            raise MCPAuthorizationError(f"unknown tool: {tool_name}")
        # Every required scope must be granted (render tools require RENDER, etc.).
        missing = meta.scopes - identity.scopes
        if missing:
            raise MCPAuthorizationError(
                f"client {identity.subject!r} lacks scope(s) "
                f"{sorted(s.value for s in missing)} for tool {tool_name!r}"
            )
        book_id = arguments.get("book_id")
        if not identity.allows_book(str(book_id) if book_id else None):
            raise MCPAuthorizationError(
                f"client {identity.subject!r} is not permitted on book {book_id!r}"
            )
        if self._next is not None:
            await self._next(tool_name, arguments)

    def for_identity(self, identity: ClientIdentity) -> BoundAuthorizer:
        """A :class:`ToolAuthorizer` bound to one identity (what a session holds)."""
        return BoundAuthorizer(self, identity)


@dataclass(frozen=True, slots=True)
class BoundAuthorizer:
    """A :class:`ScopedAuthorizer` partially applied to a single identity."""

    _scoped: ScopedAuthorizer
    _identity: ClientIdentity

    async def authorize(self, tool_name: str, arguments: dict[str, Any]) -> None:
        await self._scoped.authorize_as(self._identity, tool_name, arguments)


class ContextScopedAuthorizer:
    """A :class:`~app.mcp.server.ToolAuthorizer` that reads the request identity.

    The streamable-HTTP transport carries one authorizer for the whole app, but
    the *caller* varies per request (a read-only token vs a full token). This
    authorizer resolves the identity from the per-request contextvar that
    :class:`IdentityMiddleware` sets, then delegates to the
    :class:`ScopedAuthorizer`. With no identity in context (a misconfigured
    deployment) it denies — fail safe.
    """

    def __init__(self, scoped: ScopedAuthorizer) -> None:
        self._scoped = scoped

    async def authorize(self, tool_name: str, arguments: dict[str, Any]) -> None:
        identity = current_identity()
        if identity is None:
            raise MCPAuthorizationError("no resolved client identity for this request")
        await self._scoped.authorize_as(identity, tool_name, arguments)


class IdentityMiddleware:
    """ASGI middleware resolving the request's bearer into a :class:`ClientIdentity`.

    Sits *inside* :class:`app.mcp.server.BearerAuthMiddleware` (which already
    gated the shared token) and sets the per-request identity contextvar from the
    ``Authorization`` header via the injected :class:`IdentityResolver`. A
    resolution failure (unrecognised token when a table is configured) returns a
    403 before the MCP layer runs. Non-HTTP scopes pass through untouched.
    """

    def __init__(self, app: Any, *, resolver: IdentityResolver) -> None:
        self._app = app
        self._resolver = resolver

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        bearer = headers.get(b"authorization", b"").decode("latin-1") or None
        try:
            identity = await self._resolver.resolve(bearer)
        except MCPAuthorizationError as exc:
            await self._reject(send, str(exc))
            return
        token = _CURRENT_IDENTITY.set(identity)
        try:
            await self._app(scope, receive, send)
        finally:
            _CURRENT_IDENTITY.reset(token)

    @staticmethod
    async def _reject(send: Any, detail: str) -> None:
        import json

        body = json.dumps({"error": "forbidden", "detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "BoundAuthorizer",
    "ClientIdentity",
    "ContextScopedAuthorizer",
    "IdentityMiddleware",
    "IdentityResolver",
    "ScopedAuthorizer",
    "StaticIdentityResolver",
    "TokenGrant",
    "current_identity",
]
