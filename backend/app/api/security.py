"""Auth primitives — legacy-compatible JWT + password helpers (kinora.md §6).

This module is the **stable, backward-compatible surface** the gateway and the
SSE/WS transports already import (``app.api.deps``, ``app.api.routes.events``).
Its signatures must not change. The richer production crypto now lives in
:mod:`app.core.security` (pluggable hashing, password policy, TOTP, recovery
codes, API-key fingerprinting) and the stateful token machinery in
:mod:`app.auth.tokens`; this module re-exports the password helpers from there so
there is exactly one hashing implementation, and keeps the simple HS256
access-token issue/verify the existing Bearer flow relies on.

Tokens minted here carry only ``sub``/``iat``/``exp`` and remain interoperable
with :class:`app.auth.tokens.TokenService` (which reads the same claims and adds
optional ones).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

# Re-export the single password-hashing implementation (pluggable, bcrypt by
# default). Keeping these names here means every existing importer is unchanged.
from app.core.config import Settings
from app.core.security import hash_password, verify_password


class TokenError(Exception):
    """Raised when an access token is missing, malformed, or expired."""


class TokenData(BaseModel):
    """The validated claims carried by an access token."""

    sub: str
    exp: int
    iat: int


def create_access_token(
    subject: str, settings: Settings, *, expires_in_s: int | None = None
) -> str:
    """Issue a signed JWT for ``subject`` (the user id) — legacy-compatible.

    Carries only the claims the existing verifier reads. For the richer claim set
    (session id, roles, scopes, tenant) use :class:`app.auth.tokens.TokenService`.
    """
    now = datetime.now(UTC)
    ttl = settings.access_token_ttl_s if expires_in_s is None else expires_in_s
    expire = now + timedelta(seconds=ttl)
    payload = {"sub": subject, "iat": int(now.timestamp()), "exp": int(expire.timestamp())}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_access_token(token: str, settings: Settings) -> TokenData:
    """Decode and validate an access token; raise :class:`TokenError` on failure.

    Tolerates tokens that carry an ``aud``/``iss`` (minted by the new
    :class:`TokenService`): audience verification is disabled here so both issuers
    interoperate, and only the ``sub``/``exp``/``iat`` core is required.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("invalid token") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise TokenError("token missing subject")
    return TokenData(sub=subject, exp=int(claims.get("exp", 0)), iat=int(claims.get("iat", 0)))


__all__ = [
    "TokenData",
    "TokenError",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "verify_password",
]
