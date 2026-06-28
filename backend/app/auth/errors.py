"""Typed auth-domain exceptions.

These are *domain* errors raised by the :mod:`app.auth` services. The route layer
maps them onto the gateway's :class:`app.api.errors.APIError` envelope; keeping
them separate from the HTTP layer means the services stay framework-agnostic and
unit-testable without FastAPI.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base class for every auth-domain failure.

    Carries a stable ``code`` (the machine-readable error type the gateway emits)
    and an HTTP ``status`` so the route layer can translate without a big
    if/elif ladder.
    """

    code = "auth_error"
    status = 400

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class InvalidCredentials(AuthError):  # noqa: N818
    """Wrong email/password (kept deliberately vague to avoid user enumeration)."""

    code = "invalid_credentials"
    status = 401


class AccountLocked(AuthError):  # noqa: N818
    """Too many failed attempts; the account is temporarily locked."""

    code = "account_locked"
    status = 429

    def __init__(self, message: str | None = None, *, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AccountDisabled(AuthError):  # noqa: N818
    """The account has been administratively disabled."""

    code = "account_disabled"
    status = 403


class EmailTaken(AuthError):  # noqa: N818
    """Registration collided with an existing account."""

    code = "email_taken"
    status = 409


class WeakPassword(AuthError):  # noqa: N818
    """The proposed password failed the strength policy."""

    code = "weak_password"
    status = 422

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems) or "password too weak")
        self.problems = problems


class MfaRequired(AuthError):  # noqa: N818
    """Login needs a second factor; the client must present a TOTP/recovery code."""

    code = "mfa_required"
    status = 401

    def __init__(self, message: str | None = None, *, mfa_token: str | None = None) -> None:
        super().__init__(message)
        self.mfa_token = mfa_token


class MfaInvalid(AuthError):  # noqa: N818
    """The presented second factor was wrong."""

    code = "mfa_invalid"
    status = 401


class MfaAlreadyEnrolled(AuthError):  # noqa: N818
    """MFA enrolment requested but the account already has MFA enabled."""

    code = "mfa_already_enrolled"
    status = 409


class MfaNotEnrolled(AuthError):  # noqa: N818
    """An MFA operation was requested but the account has no MFA configured."""

    code = "mfa_not_enrolled"
    status = 409


class TokenInvalid(AuthError):  # noqa: N818
    """A token is missing, malformed, expired, or revoked."""

    code = "token_invalid"
    status = 401


class TokenReused(AuthError):  # noqa: N818
    """A refresh token was replayed — the whole family is revoked (breach signal)."""

    code = "token_reuse_detected"
    status = 401


class PermissionDenied(AuthError):  # noqa: N818
    """The principal lacks the required role/permission/scope, or crosses tenants."""

    code = "permission_denied"
    status = 403


class ApiKeyInvalid(AuthError):  # noqa: N818
    """A presented API key is unknown, revoked, or expired."""

    code = "api_key_invalid"
    status = 401


class SessionNotFound(AuthError):  # noqa: N818
    """A session referenced for revocation does not exist or is not the caller's."""

    code = "session_not_found"
    status = 404


class RateLimited(AuthError):  # noqa: N818
    """A per-identity rate limit was exceeded."""

    code = "rate_limited"
    status = 429

    def __init__(self, message: str | None = None, *, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


__all__ = [
    "AccountDisabled",
    "AccountLocked",
    "ApiKeyInvalid",
    "AuthError",
    "EmailTaken",
    "InvalidCredentials",
    "MfaAlreadyEnrolled",
    "MfaInvalid",
    "MfaNotEnrolled",
    "MfaRequired",
    "PermissionDenied",
    "RateLimited",
    "SessionNotFound",
    "TokenInvalid",
    "TokenReused",
    "WeakPassword",
]
