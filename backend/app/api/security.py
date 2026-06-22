"""Auth primitives — password hashing (bcrypt) and JWT issue/verify (kinora.md §6).

Passwords are hashed with **bcrypt** (the ``bcrypt`` library from the declared
``passlib[bcrypt]`` extra, called directly — passlib 1.7.4's backend probe is
incompatible with bcrypt ≥ 4.1 and crashes on import). Access tokens are signed
JWTs (HS256 by default) carrying the user id (``sub``), issued-at, and expiry,
using ``settings.jwt_secret`` / ``settings.jwt_alg`` / ``settings.access_token_ttl_s``.
The transport layer (``api.deps.get_current_user``) verifies the Bearer token and
loads the user; this module owns the crypto only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from pydantic import BaseModel

from app.core.config import Settings

# bcrypt hashes at most the first 72 bytes; longer inputs are truncated to it so
# registration never errors on a long passphrase (bcrypt ≥ 4.1 raises otherwise).
_BCRYPT_MAX_BYTES = 72


class TokenError(Exception):
    """Raised when an access token is missing, malformed, or expired."""


class TokenData(BaseModel):
    """The validated claims carried by an access token."""

    sub: str
    exp: int
    iat: int


def _truncate(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password`` (safe for storage)."""
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Verify ``password`` against a stored bcrypt ``hashed`` value."""
    try:
        return bcrypt.checkpw(_truncate(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str, settings: Settings, *, expires_in_s: int | None = None
) -> str:
    """Issue a signed JWT for ``subject`` (the user id)."""
    now = datetime.now(UTC)
    ttl = settings.access_token_ttl_s if expires_in_s is None else expires_in_s
    expire = now + timedelta(seconds=ttl)
    payload = {"sub": subject, "iat": int(now.timestamp()), "exp": int(expire.timestamp())}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_access_token(token: str, settings: Settings) -> TokenData:
    """Decode and validate an access token; raise :class:`TokenError` on failure."""
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
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
