"""JWT access tokens + refresh-token family rotation with reuse detection.

Two token kinds:

* **Access tokens** — short(ish)-lived signed JWTs carrying the user id (``sub``),
  plus additive claims (``jti``, ``sid`` session id, ``roles``, ``scopes``,
  ``tenant``, ``typ="access"``). They remain **backward compatible** with the
  legacy ``app.api.security.create_access_token`` output: ``app.api.deps`` only
  reads ``sub`` and the standard ``exp``/``iat``, so an old client and the new
  issuer interoperate. New code can read the richer claims via
  :class:`AccessClaims`.

* **Refresh tokens** — opaque high-entropy secrets (never JWTs) handed back to
  the client and stored **only as a SHA-256 digest**. Every refresh **rotates**:
  the presented token is consumed and a fresh one is issued in the same
  *family*. Presenting an already-consumed token is a replay — the classic
  stolen-refresh-token signal — and **revokes the entire family** (§12 security).

This module owns the *stateless* crypto + claim shaping. The *stateful* parts
(persisting refresh-token rows, the revocation set, reuse bookkeeping) live in
:class:`app.auth.repositories` / :class:`app.auth.service`; :class:`TokenService`
takes a small persistence port so it stays unit-testable without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt

from app.core.config import Settings
from app.core.security import generate_token, sha256_hex

# --------------------------------------------------------------------------- #
# Access-token claims
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class AccessClaims:
    """The validated claims carried by an access token (superset of the legacy set)."""

    sub: str
    exp: int
    iat: int
    jti: str = ""
    sid: str | None = None
    typ: str = "access"
    roles: tuple[str, ...] = field(default_factory=tuple)
    scopes: tuple[str, ...] = field(default_factory=tuple)
    tenant: str | None = None

    @property
    def user_id(self) -> str:
        """Alias for ``sub`` — the authenticated user id."""
        return self.sub


@dataclass(slots=True)
class IssuedRefreshToken:
    """A freshly-issued refresh token: the opaque secret + its storage fields."""

    #: The opaque token string handed to the client (shown once, then digested).
    token: str
    #: The SHA-256 digest persisted for lookup (the plaintext is never stored).
    digest: str
    #: The family this token belongs to (rotation chains share a family id).
    family_id: str
    #: When the token expires (UTC).
    expires_at: datetime


class RevocationStore(Protocol):
    """A minimal port for access-token (``jti``) revocation checks.

    Access tokens are stateless, so "logout-now" for a *specific* access token is
    a denylist of its ``jti`` until its natural expiry. The default
    implementation is Redis-backed (see :class:`app.auth.service`); tests pass an
    in-memory fake.
    """

    async def is_revoked(self, jti: str) -> bool:
        """Whether the access token ``jti`` has been explicitly revoked."""
        ...

    async def revoke(self, jti: str, *, ttl_s: int) -> None:
        """Denylist ``jti`` for ``ttl_s`` seconds (its remaining lifetime)."""
        ...


class _NullRevocationStore:
    """A no-op revocation store (nothing is ever revoked)."""

    async def is_revoked(self, jti: str) -> bool:  # noqa: D102
        return False

    async def revoke(self, jti: str, *, ttl_s: int) -> None:  # noqa: D102
        return None


# --------------------------------------------------------------------------- #
# TokenService
# --------------------------------------------------------------------------- #


class TokenService:
    """Issue + verify access tokens and mint/rotate refresh tokens.

    Stateless by construction: the only state it consults is the injected
    :class:`RevocationStore` (for the access-token denylist). Refresh-token
    persistence and family-reuse bookkeeping live in the service layer that owns
    a DB session; this class produces the values that layer stores.
    """

    def __init__(self, settings: Settings, *, revocations: RevocationStore | None = None) -> None:
        self._settings = settings
        self._revocations = revocations or _NullRevocationStore()

    # -- access tokens ------------------------------------------------------- #

    def issue_access_token(
        self,
        subject: str,
        *,
        session_id: str | None = None,
        jti: str | None = None,
        roles: Sequence[str] = (),
        scopes: Sequence[str] = (),
        tenant: str | None = None,
        expires_in_s: int | None = None,
    ) -> tuple[str, AccessClaims]:
        """Mint a signed access token and return it with its decoded claims.

        The wire token stays compatible with the legacy verifier: only ``sub`` +
        the standard ``exp``/``iat`` are required to authenticate; everything else
        is additive.
        """
        now = datetime.now(UTC)
        ttl = self._settings.access_token_ttl_s if expires_in_s is None else expires_in_s
        exp = now + timedelta(seconds=ttl)
        token_id = jti or generate_token(16)
        payload: dict[str, Any] = {
            "sub": subject,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": token_id,
            "typ": "access",
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
        }
        if session_id is not None:
            payload["sid"] = session_id
        if roles:
            payload["roles"] = list(roles)
        if scopes:
            payload["scopes"] = list(scopes)
        if tenant is not None:
            payload["tenant"] = tenant
        token = jwt.encode(payload, self._settings.jwt_secret, algorithm=self._settings.jwt_alg)
        claims = AccessClaims(
            sub=subject,
            exp=payload["exp"],
            iat=payload["iat"],
            jti=token_id,
            sid=session_id,
            roles=tuple(roles),
            scopes=tuple(scopes),
            tenant=tenant,
        )
        return token, claims

    def decode_access_token(self, token: str, *, verify_aud: bool = True) -> AccessClaims:
        """Decode + validate an access token into :class:`AccessClaims`.

        Tokens minted by the legacy issuer (no ``aud``/``iss``) still verify:
        audience verification is skipped when the token carries no ``aud`` claim,
        so old tokens in flight during a deploy keep working.
        """
        from app.auth.errors import TokenInvalid

        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise TokenInvalid("malformed token") from exc
        has_aud = "aud" in unverified
        try:
            claims = jwt.decode(
                token,
                self._settings.jwt_secret,
                algorithms=[self._settings.jwt_alg],
                audience=self._settings.jwt_audience if has_aud else None,
                options={"verify_aud": bool(verify_aud and has_aud)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenInvalid("token expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenInvalid("invalid token") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenInvalid("token missing subject")
        return AccessClaims(
            sub=subject,
            exp=int(claims.get("exp", 0)),
            iat=int(claims.get("iat", 0)),
            jti=str(claims.get("jti", "")),
            sid=claims.get("sid"),
            typ=str(claims.get("typ", "access")),
            roles=tuple(claims.get("roles", []) or []),
            scopes=tuple(claims.get("scopes", []) or []),
            tenant=claims.get("tenant"),
        )

    async def verify_access_token(self, token: str) -> AccessClaims:
        """Decode an access token **and** reject it if its ``jti`` is revoked."""
        from app.auth.errors import TokenInvalid

        claims = self.decode_access_token(token)
        if claims.jti and await self._revocations.is_revoked(claims.jti):
            raise TokenInvalid("token revoked")
        return claims

    async def revoke_access_jti(self, claims: AccessClaims) -> None:
        """Denylist an access token's ``jti`` for its remaining lifetime."""
        if not claims.jti:
            return
        remaining = max(claims.exp - int(datetime.now(UTC).timestamp()), 1)
        await self._revocations.revoke(claims.jti, ttl_s=remaining)

    # -- mfa challenge tokens (short-lived, password-step-only) ------------- #

    def issue_mfa_challenge(self, subject: str, *, session_hint: str | None = None) -> str:
        """Mint a short-lived token proving the password step of a 2FA login."""
        now = datetime.now(UTC)
        exp = now + timedelta(seconds=self._settings.mfa_challenge_ttl_s)
        payload: dict[str, Any] = {
            "sub": subject,
            "typ": "mfa_challenge",
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iss": self._settings.jwt_issuer,
        }
        if session_hint:
            payload["sh"] = session_hint
        return jwt.encode(payload, self._settings.jwt_secret, algorithm=self._settings.jwt_alg)

    def decode_mfa_challenge(self, token: str) -> str:
        """Validate an MFA-challenge token; return the user id it proves."""
        from app.auth.errors import MfaInvalid

        try:
            claims = jwt.decode(
                token,
                self._settings.jwt_secret,
                algorithms=[self._settings.jwt_alg],
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise MfaInvalid("invalid or expired mfa challenge") from exc
        if claims.get("typ") != "mfa_challenge":
            raise MfaInvalid("not an mfa challenge token")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise MfaInvalid("mfa challenge missing subject")
        return subject

    # -- refresh tokens ------------------------------------------------------ #

    def issue_refresh_token(self, *, family_id: str | None = None) -> IssuedRefreshToken:
        """Mint an opaque refresh token (and its storage digest + family).

        Passing an existing ``family_id`` continues a rotation chain; omitting it
        starts a new family (a fresh login / a recovered breach).
        """
        token = generate_token(48)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.refresh_token_ttl_s)
        return IssuedRefreshToken(
            token=token,
            digest=sha256_hex(token),
            family_id=family_id or generate_token(16),
            expires_at=expires_at,
        )

    @staticmethod
    def refresh_digest(token: str) -> str:
        """The lookup digest for a presented refresh token."""
        return sha256_hex(token)


__all__ = [
    "AccessClaims",
    "IssuedRefreshToken",
    "RevocationStore",
    "TokenService",
]
