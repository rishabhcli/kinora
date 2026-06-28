"""Typed error hierarchy for the notifications platform.

Transports raise these so the dispatcher / webhook engine can tell a *transient*
failure (worth a backoff-retry, kinora.md §12.1) from a *permanent* one (a bad
recipient / 4xx — never retried, dead-lettered immediately). Keeping the
distinction in the type system means the retry logic is a single ``isinstance``
check rather than scattered string-matching on error messages.
"""

from __future__ import annotations


class NotificationError(Exception):
    """Base class for every notifications-platform error."""


class TransportError(NotificationError):
    """A channel transport failed to deliver a message.

    ``retryable`` drives the §12.1 retry decision: transient failures back off
    and re-attempt; permanent ones are dead-lettered on the first failure.
    """

    retryable: bool = True

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class TransientTransportError(TransportError):
    """A temporary failure (network blip, 5xx, throttling) — safe to retry."""

    retryable = True


class PermanentTransportError(TransportError):
    """A non-recoverable failure (bad address, 4xx, malformed payload) — do not retry."""

    retryable = False


class CircuitOpenError(NotificationError):
    """Raised when a call is rejected because the channel's circuit is open (§12).

    This is itself *transient* from the caller's perspective: the endpoint is
    temporarily fenced off and the work should be retried later, not dropped.
    """

    def __init__(self, target: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(f"circuit open for {target!r}")
        self.target = target
        self.retry_after_s = retry_after_s


class TemplateNotFoundError(NotificationError):
    """No template registered for an (event, channel, locale) lookup."""


class PreferencesError(NotificationError):
    """A malformed or contradictory preference configuration."""


__all__ = [
    "CircuitOpenError",
    "NotificationError",
    "PermanentTransportError",
    "PreferencesError",
    "TemplateNotFoundError",
    "TransientTransportError",
    "TransportError",
]
