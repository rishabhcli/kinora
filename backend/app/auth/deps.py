"""FastAPI dependencies for the auth/security plane (kinora.md §6, §12).

These compose **on top of** the existing Bearer-token flow in
:mod:`app.api.deps` without changing it. Two authentication paths feed one
:class:`~app.auth.rbac.Principal`:

* an ``Authorization: Bearer <jwt>`` access token (interactive users), verified
  through :class:`~app.auth.tokens.TokenService` (so the ``jti`` revocation
  denylist and the session-active check apply); or
* an ``X-API-Key: kino_sk_...`` header (headless callers), verified through
  :meth:`AuthService.authenticate_api_key`.

On top of the principal sit small dependency factories — :func:`require_permission`,
:func:`require_scope`, :func:`require_role`, :func:`require_tenant` — that gate a
route on the caller's effective authorisation. They translate the auth-domain
:class:`~app.auth.errors.AuthError` into the gateway's typed
:class:`~app.api.errors.APIError` envelope so the JSON error contract is uniform.

The legacy :data:`app.api.deps.CurrentUser` keeps working unchanged; new routes
opt into the richer model by depending on :data:`CurrentPrincipal`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Request

from app.api.deps import ContainerDep
from app.api.errors import APIError
from app.auth.errors import AuthError
from app.auth.rbac import Principal
from app.auth.service import AuthService, LoginContext
from app.auth.tokens import TokenService


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP, honouring a single ``X-Forwarded-For`` hop."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client is not None else None


def login_context(request: Request) -> LoginContext:
    """Build the :class:`LoginContext` (IP + UA) for audit/device tracking."""
    return LoginContext(ip=_client_ip(request), user_agent=request.headers.get("user-agent"))


LoginCtx = Annotated[LoginContext, Depends(login_context)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def get_principal(
    request: Request,
    container: ContainerDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Resolve the caller into a :class:`Principal` from a token **or** an API key.

    Tries the API key first (an explicit ``X-API-Key`` header signals intent),
    then the Bearer access token. Raises a 401 ``APIError`` when neither yields a
    valid principal.
    """
    service: AuthService = container.auth_service
    if x_api_key:
        try:
            return await service.authenticate_api_key(x_api_key)
        except AuthError as exc:
            raise APIError(exc.code, exc.message, status=exc.status) from exc

    token = _bearer_token(request)
    if not token:
        raise APIError("unauthorized", "missing bearer token or api key", status=401)
    from app.auth.lockout import RevocationStore

    tokens = TokenService(container.settings, revocations=RevocationStore(container.redis))
    try:
        claims = await tokens.verify_access_token(token)
    except AuthError as exc:
        raise APIError("unauthorized", exc.message, status=401) from exc
    # If the token carries a session id, ensure the session is still active so a
    # revoked/expired session can't keep authenticating with a live access token.
    if claims.sid is not None:
        from app.auth.repositories import AuthSessionRepo

        async with container.session_factory() as db:
            if not await AuthSessionRepo(db).is_active(claims.sid):
                raise APIError("unauthorized", "session revoked", status=401)
    # Rebuild the principal from the DB so role/permission changes take effect
    # without waiting for the access token to expire.
    return await service.build_principal_for_user(claims.sub, session_id=claims.sid)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_permission(*permissions: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that requires the caller to hold ALL ``permissions``.

    Usage::

        @router.post("/x", dependencies=[Depends(require_permission("books:write"))])
    """

    async def _dep(principal: CurrentPrincipal) -> Principal:
        if not principal.has_all(permissions):
            raise APIError(
                "permission_denied",
                f"requires permission(s): {', '.join(permissions)}",
                status=403,
                detail={"required": list(permissions)},
            )
        return principal

    return _dep


def require_any_permission(*permissions: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that requires AT LEAST ONE of ``permissions``."""

    async def _dep(principal: CurrentPrincipal) -> Principal:
        if not principal.has_any(permissions):
            raise APIError(
                "permission_denied",
                f"requires one of: {', '.join(permissions)}",
                status=403,
                detail={"any_of": list(permissions)},
            )
        return principal

    return _dep


def require_scope(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    """Alias of :func:`require_permission` (scopes and permissions share a check)."""
    return require_permission(*scopes)


def require_role(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that requires the caller to hold AT LEAST ONE of ``roles``."""

    async def _dep(principal: CurrentPrincipal) -> Principal:
        if not any(principal.has_role(r) for r in roles):
            raise APIError(
                "permission_denied",
                f"requires role(s): {', '.join(roles)}",
                status=403,
                detail={"any_role": list(roles)},
            )
        return principal

    return _dep


#: A ready-made admin gate (the RBAC/audit/user-admin endpoints depend on it).
require_admin = require_any_permission("admin:rbac", "admin:users", "admin:audit")


__all__ = [
    "CurrentPrincipal",
    "LoginCtx",
    "get_principal",
    "login_context",
    "require_admin",
    "require_any_permission",
    "require_permission",
    "require_role",
    "require_scope",
]
