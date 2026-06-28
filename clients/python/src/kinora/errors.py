"""Typed error hierarchy for the Kinora Python SDK.

Every non-2xx response (and transport failure) is raised as a subclass of
:class:`KinoraError`, so callers can ``except NotFoundError`` or branch on
``err.status`` / ``err.type``. The backend ships a stable envelope
``{"error": {type, message, detail?}}`` (see ``backend/app/api/errors.py``);
:func:`error_for_status` maps that onto these classes by status + type string.
"""

from __future__ import annotations

from typing import Any


class KinoraError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        type: str | None = None,
        detail: dict[str, Any] | None = None,
        body: str | None = None,
        request: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.detail = detail
        self.body = body
        self.request = request

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = f" ({self.request})" if self.request else ""
        code = f" [{self.status}]" if self.status else ""
        return f"{self.message}{code}{where}"


class AuthError(KinoraError):
    """401 — missing/invalid bearer token, or wrong credentials."""


class ForbiddenError(KinoraError):
    """403 — authenticated but not allowed (e.g. a local-only endpoint in prod)."""


class NotFoundError(KinoraError):
    """404 — resource not found / not owned by the caller."""


class ConflictError(KinoraError):
    """409 — a conflict (email already taken, etc.)."""


class LiveVideoDisabledError(KinoraError):
    """409 — live video generation is gated off (KINORA_LIVE_VIDEO)."""


class BudgetExceededError(KinoraError):
    """402 — the hard video-second budget cap was reached."""


class UploadError(KinoraError):
    """413/415 — upload too large / unsupported media type."""


class ValidationError(KinoraError):
    """422 — request validation failed. ``detail['errors']`` lists bad fields."""


class RateLimitError(KinoraError):
    """429 — rate limited or quota exceeded. Honor ``retry_after_s``."""

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


class ProviderError(KinoraError):
    """502 — an upstream model/provider failure."""


class ServerError(KinoraError):
    """5xx — a server error."""


class TimeoutError(KinoraError):
    """The request timed out (client-side)."""


class NetworkError(KinoraError):
    """A network/transport failure (DNS, connection refused, etc.)."""


def error_for_status(
    status: int,
    body: dict[str, Any] | None,
    raw: str | None,
    request: str | None,
    retry_after_s: float | None,
) -> KinoraError:
    """Map an HTTP status + backend error envelope onto the right error class."""
    err = (body or {}).get("error") if isinstance(body, dict) else None
    type_ = err.get("type") if isinstance(err, dict) else None
    raw_message = err.get("message") if isinstance(err, dict) else None
    message: str = str(raw_message) if raw_message else f"request failed with status {status}"
    detail = err.get("detail") if isinstance(err, dict) else None
    common: dict[str, Any] = {
        "status": status,
        "type": type_,
        "detail": detail,
        "body": raw,
        "request": request,
    }

    if type_ == "live_video_disabled":
        return LiveVideoDisabledError(message, **common)
    if type_ == "budget_exceeded":
        return BudgetExceededError(message, **common)
    if type_ == "provider_error":
        return ProviderError(message, **common)

    mapping: dict[int, type[KinoraError]] = {
        401: AuthError,
        402: BudgetExceededError,
        403: ForbiddenError,
        404: NotFoundError,
        409: ConflictError,
        413: UploadError,
        415: UploadError,
        422: ValidationError,
        502: ProviderError,
    }
    if status == 429:
        return RateLimitError(message, retry_after_s=retry_after_s, **common)
    cls = mapping.get(status)
    if cls is not None:
        return cls(message, **common)
    if status >= 500:
        return ServerError(message, **common)
    return KinoraError(message, **common)


__all__ = [
    "AuthError",
    "BudgetExceededError",
    "ConflictError",
    "ForbiddenError",
    "KinoraError",
    "LiveVideoDisabledError",
    "NetworkError",
    "NotFoundError",
    "ProviderError",
    "RateLimitError",
    "ServerError",
    "TimeoutError",
    "UploadError",
    "ValidationError",
    "error_for_status",
]
