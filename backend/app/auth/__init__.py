"""Production auth & security plane for the Kinora backend (kinora.md §6, §12).

This package layers a full authentication/authorisation system on top of the
minimal register/login/me flow the gateway shipped with, **without breaking** the
existing Bearer-token contract that ``app.api.deps.get_current_user``, the
SSE/WS transports, and the MCP authorizer depend on:

* :mod:`app.auth.tokens` — JWT access tokens (HS256, ``sub`` = user id, backward
  compatible) plus opaque refresh tokens with **token-family rotation and
  reuse-detection** (a replayed refresh token revokes the whole family).
* :mod:`app.auth.passwords` — password set/verify/change/reset with the pluggable
  hasher and strength policy from :mod:`app.core.security`.
* :mod:`app.auth.mfa` / :mod:`app.auth.recovery` — TOTP enrolment + verification
  and single-use recovery codes.
* :mod:`app.auth.api_keys` — first-class API keys with scopes for headless callers.
* :mod:`app.auth.rbac` — roles, permissions, scopes, and per-tenant isolation.
* :mod:`app.auth.sessions` — session lifecycle with device tracking + revocation.
* :mod:`app.auth.lockout` — login throttling and account lockout.
* :mod:`app.auth.audit` — a structured security audit log.
* :mod:`app.auth.service` — the :class:`AuthService` orchestrator that the route
  layer calls.

Everything here is DB/Redis-backed but constructed lazily through the
composition root, so importing this package opens no sockets.
"""

from __future__ import annotations

from app.auth.errors import AuthError
from app.auth.rbac import Principal
from app.auth.service import AuthService, LoginContext, MfaEnrollment, TokenBundle
from app.auth.tokens import AccessClaims, TokenService

__all__ = [
    "AccessClaims",
    "AuthError",
    "AuthService",
    "LoginContext",
    "MfaEnrollment",
    "Principal",
    "TokenBundle",
    "TokenService",
]
