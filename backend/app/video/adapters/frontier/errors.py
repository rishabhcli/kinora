"""Shared error taxonomy for the frontier hosted video adapters.

Every frontier provider (Runway, Luma, Pika, Kling, Veo, Sora) speaks a *different*
HTTP error dialect. This module collapses all of them into one small, branchable
taxonomy so the render pipeline / router can decide *why* a render failed without
parsing per-provider strings.

The taxonomy intentionally **subclasses the existing provider exception hierarchy**
(:mod:`app.providers.errors`), so a frontier adapter is a drop-in
:class:`~app.providers.video_router.VideoBackend`: the
:class:`~app.providers.video_router.VideoRouter` already branches on
``retryable`` / :class:`~app.providers.errors.LiveVideoDisabled` /
:class:`~app.providers.errors.ProviderBadRequest`, and inherits the right behaviour
for free. A :class:`FrontierError` *is a* :class:`~app.providers.errors.ProviderError`.

The canonical, provider-agnostic *reason* lives on :class:`FrontierErrorCode` (a
``StrEnum``) so telemetry can aggregate "why frontier renders fail" across providers
even though each maps from its own native codes.
"""

from __future__ import annotations

from enum import StrEnum

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)


class FrontierErrorCode(StrEnum):
    """Canonical, provider-agnostic failure reasons for a frontier render.

    A single vocabulary every adapter maps its native error codes into, so the
    pipeline and dashboards reason about *one* taxonomy instead of six.
    """

    #: Bad/missing/forbidden credentials, or the org lacks access to the model.
    AUTH = "auth"
    #: The request body is invalid for this provider (unsupported param/value).
    INVALID_REQUEST = "invalid_request"
    #: A constraint the adapter validates locally (duration/resolution/aspect/
    #: prompt-length/reference-count outside the provider's declared capability).
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    #: Throttled — too many requests / concurrent jobs. Retryable after a backoff.
    RATE_LIMITED = "rate_limited"
    #: Account is out of credits / over quota. NOT retryable (won't fix on retry).
    QUOTA_EXHAUSTED = "quota_exhausted"
    #: The provider's content filter rejected the prompt or a reference image.
    CONTENT_MODERATED = "content_moderated"
    #: The job was accepted but the provider reported it FAILED/ERROR terminally.
    JOB_FAILED = "job_failed"
    #: The provider cancelled the job (or we did, then observed it CANCELED).
    JOB_CANCELED = "job_canceled"
    #: We polled past the deadline without a terminal status.
    TIMEOUT = "timeout"
    #: 5xx / network blip / transport error. Retryable.
    SERVER_ERROR = "server_error"
    #: A successful response whose body we could not parse into the expected shape.
    BAD_RESPONSE = "bad_response"
    #: Anything we could not classify.
    UNKNOWN = "unknown"


#: The canonical reasons the router/pipeline may safely retry by trying again
#: (same backend) or failing over (next backend).
_RETRYABLE_CODES: frozenset[FrontierErrorCode] = frozenset(
    {
        FrontierErrorCode.RATE_LIMITED,
        FrontierErrorCode.TIMEOUT,
        FrontierErrorCode.SERVER_ERROR,
    }
)


class FrontierError(ProviderError):
    """Base class for every frontier-adapter failure.

    Carries the originating :class:`FrontierErrorCode` plus the *provider* name and
    its *native* error code (for cross-referencing in the provider's own logs).
    Being a :class:`~app.providers.errors.ProviderError`, it slots straight into the
    router's failover/health logic.
    """

    code_enum: FrontierErrorCode = FrontierErrorCode.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        code: str | None = None,
        code_enum: FrontierErrorCode | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        if code_enum is not None:
            self.code_enum = code_enum
        self.provider = provider
        # Default retryability follows the canonical code unless overridden.
        if retryable is None:
            retryable = self.code_enum in _RETRYABLE_CODES
        super().__init__(
            message,
            code=code,
            status_code=status_code,
            request_id=request_id,
            retryable=retryable,
        )

    def __str__(self) -> str:  # pragma: no cover - thin formatting
        base = super().__str__()
        bits = [base, f"reason={self.code_enum.value}"]
        if self.provider:
            bits.append(f"provider={self.provider}")
        return " | ".join(bits)


# --------------------------------------------------------------------------- #
# Concrete taxonomy members (also slotted under the existing provider types so
# the router's isinstance/retryable branches keep working unchanged).
# --------------------------------------------------------------------------- #


class FrontierAuthError(FrontierError, AuthenticationError):
    """Bad/missing credentials, or the org lacks access to the requested model."""

    code_enum = FrontierErrorCode.AUTH


class FrontierBadRequest(FrontierError, ProviderBadRequest):  # noqa: N818
    """The request body is invalid for this provider (non-retryable)."""

    code_enum = FrontierErrorCode.INVALID_REQUEST


class FrontierUnsupportedCapability(FrontierError, ProviderBadRequest):  # noqa: N818
    """A locally-validated capability violation (non-retryable).

    Raised *before* any network call when a canonical request asks for something
    the provider's :class:`~app.video.adapters.frontier.types.CapabilityProfile`
    does not allow (e.g. a 30s clip on a model capped at 10s).
    """

    code_enum = FrontierErrorCode.UNSUPPORTED_CAPABILITY


class FrontierRateLimited(FrontierError, RateLimited):  # noqa: N818
    """Throttled by the provider (retryable after a backoff)."""

    code_enum = FrontierErrorCode.RATE_LIMITED


class FrontierQuotaExhausted(FrontierError, ProviderBadRequest):  # noqa: N818
    """Out of credits / over quota — NOT retryable (a retry fails identically)."""

    code_enum = FrontierErrorCode.QUOTA_EXHAUSTED


class FrontierContentModerated(FrontierError, ProviderBadRequest):  # noqa: N818
    """The provider's content filter rejected the prompt/reference (non-retryable)."""

    code_enum = FrontierErrorCode.CONTENT_MODERATED


class FrontierJobFailed(FrontierError):  # noqa: N818
    """The accepted job reported a terminal FAILED/ERROR status (non-retryable)."""

    code_enum = FrontierErrorCode.JOB_FAILED


class FrontierJobCanceled(FrontierError):  # noqa: N818
    """The job was canceled (by the provider or us) (non-retryable)."""

    code_enum = FrontierErrorCode.JOB_CANCELED


class FrontierTimeout(FrontierError, ProviderTimeout):  # noqa: N818
    """Polled past the deadline without a terminal status (retryable)."""

    code_enum = FrontierErrorCode.TIMEOUT


class FrontierServerError(FrontierError, TransientProviderError):
    """5xx / transport blip (retryable)."""

    code_enum = FrontierErrorCode.SERVER_ERROR


class FrontierBadResponse(FrontierError):  # noqa: N818
    """A response we received but could not parse into the expected shape."""

    code_enum = FrontierErrorCode.BAD_RESPONSE


#: Map an HTTP status (when no more-specific provider code applies) to a canonical
#: reason. Providers override with their own code maps; this is the fallback.
_STATUS_TO_CODE: dict[int, FrontierErrorCode] = {
    400: FrontierErrorCode.INVALID_REQUEST,
    401: FrontierErrorCode.AUTH,
    403: FrontierErrorCode.AUTH,
    404: FrontierErrorCode.INVALID_REQUEST,
    408: FrontierErrorCode.TIMEOUT,
    422: FrontierErrorCode.INVALID_REQUEST,
    429: FrontierErrorCode.RATE_LIMITED,
}


def code_for_status(status: int) -> FrontierErrorCode:
    """Map a bare HTTP status to a canonical reason (server errors → SERVER_ERROR)."""
    if status in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status]
    if status >= 500:
        return FrontierErrorCode.SERVER_ERROR
    return FrontierErrorCode.UNKNOWN


#: Constructor for each canonical reason — the single place that turns a
#: :class:`FrontierErrorCode` into the right concrete exception.
_CODE_TO_EXC: dict[FrontierErrorCode, type[FrontierError]] = {
    FrontierErrorCode.AUTH: FrontierAuthError,
    FrontierErrorCode.INVALID_REQUEST: FrontierBadRequest,
    FrontierErrorCode.UNSUPPORTED_CAPABILITY: FrontierUnsupportedCapability,
    FrontierErrorCode.RATE_LIMITED: FrontierRateLimited,
    FrontierErrorCode.QUOTA_EXHAUSTED: FrontierQuotaExhausted,
    FrontierErrorCode.CONTENT_MODERATED: FrontierContentModerated,
    FrontierErrorCode.JOB_FAILED: FrontierJobFailed,
    FrontierErrorCode.JOB_CANCELED: FrontierJobCanceled,
    FrontierErrorCode.TIMEOUT: FrontierTimeout,
    FrontierErrorCode.SERVER_ERROR: FrontierServerError,
    FrontierErrorCode.BAD_RESPONSE: FrontierBadResponse,
    FrontierErrorCode.UNKNOWN: FrontierError,
}


def build_error(
    code: FrontierErrorCode,
    message: str,
    *,
    provider: str | None = None,
    native_code: str | None = None,
    status_code: int | None = None,
    request_id: str | None = None,
    retry_after_s: float | None = None,
) -> FrontierError:
    """Construct the concrete :class:`FrontierError` for a canonical ``code``.

    The single factory adapters use, so the mapping reason → exception type lives
    in exactly one place. ``retry_after_s`` is attached when the concrete type is a
    :class:`~app.providers.errors.RateLimited` (it carries that field).
    """
    exc_cls = _CODE_TO_EXC.get(code, FrontierError)
    err = exc_cls(
        message,
        provider=provider,
        code=native_code,
        code_enum=code,
        status_code=status_code,
        request_id=request_id,
    )
    if retry_after_s is not None and isinstance(err, RateLimited):
        err.retry_after_s = retry_after_s
    return err


__all__ = [
    "FrontierAuthError",
    "FrontierBadRequest",
    "FrontierBadResponse",
    "FrontierContentModerated",
    "FrontierError",
    "FrontierErrorCode",
    "FrontierJobCanceled",
    "FrontierJobFailed",
    "FrontierQuotaExhausted",
    "FrontierRateLimited",
    "FrontierServerError",
    "FrontierTimeout",
    "FrontierUnsupportedCapability",
    "build_error",
    "code_for_status",
]
