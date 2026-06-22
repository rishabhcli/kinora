"""Typed exception hierarchy for the DashScope provider layer.

Every provider call funnels failures through these types so callers (agents,
the render queue, the budget service) can branch on *why* a call failed —
retryable transport blips vs. hard request errors vs. the deliberate
``LiveVideoDisabled`` gate — without parsing strings.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider-layer failure.

    Attributes:
        message: Human-readable description (never contains the API key).
        code: DashScope error ``code`` when one was returned (e.g.
            ``"InvalidParameter"``).
        status_code: HTTP status when the failure originated from an HTTP call.
        request_id: DashScope ``request_id`` for cross-referencing in their logs.
        retryable: Whether retrying the same call could plausibly succeed.
    """

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class TransientProviderError(ProviderError):
    """A transient failure (network blip, 5xx, task-poll hiccup). Safe to retry."""

    retryable = True


class ProviderTimeout(TransientProviderError):  # noqa: N818 - public name in task contract
    """The call exceeded its per-attempt timeout."""


class RateLimited(TransientProviderError):  # noqa: N818 - public name in task contract
    """The provider returned HTTP 429 / Throttling. Retry after a backoff.

    Attributes:
        retry_after_s: Server-suggested wait, parsed from ``Retry-After`` when
            present.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.retry_after_s = retry_after_s


class AuthenticationError(ProviderError):
    """Invalid / missing API key, or insufficient permissions. Not retryable."""


class ProviderBadRequest(ProviderError):  # noqa: N818 - public name in task contract
    """A 4xx the caller must fix (bad params, unsupported input). Not retryable."""


class ModelNotAvailable(ProviderBadRequest):
    """The requested model id is not recognized by the endpoint."""


class ResponseParseError(ProviderError):
    """The response was received but could not be parsed into the expected shape."""


class CircuitOpenError(TransientProviderError):
    """The circuit breaker is open; the call was rejected without being attempted."""


class LiveVideoDisabled(ProviderError):  # noqa: N818 - public name in task contract
    """Raised by ``video.render`` when ``settings.kinora_live_video`` is False.

    This is a deliberate spend gate, **not** an error condition: real Wan renders
    burn scarce, metered video-seconds, so the pipeline keeps this off while
    iterating on the non-video path. Degradation (Ken-Burns over a keyframe) is
    handled by the render worker, *not* here — this provider never fabricates a
    clip.
    """


__all__ = [
    "AuthenticationError",
    "CircuitOpenError",
    "LiveVideoDisabled",
    "ModelNotAvailable",
    "ProviderBadRequest",
    "ProviderError",
    "ProviderTimeout",
    "RateLimited",
    "ResponseParseError",
    "TransientProviderError",
]
