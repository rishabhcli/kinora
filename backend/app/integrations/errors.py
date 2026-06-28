"""Exception hierarchy for the integrations domain.

The hierarchy separates *transient* failures (retry with backoff) from
*permanent* ones (give up, surface to the user) so the sync engine can classify
without string-matching. Connector authors raise these; the sync engine and the
service layer catch them.
"""

from __future__ import annotations


class IntegrationError(RuntimeError):
    """Base for every error raised inside the integrations framework."""


class ConfigurationError(IntegrationError):
    """A connector/provider is misconfigured (missing client id/secret, etc.).

    Permanent from the sync engine's point of view: retrying will not fix it.
    """


class ConnectorError(IntegrationError):
    """A connector failed to fetch or normalize source material."""


class TransientError(ConnectorError):
    """A retryable failure — a 5xx, a timeout, a momentary network blip.

    The sync engine retries these on an exponential backoff schedule.
    """


class PermanentError(ConnectorError):
    """A non-retryable failure — a 4xx that will never succeed (bad request).

    The sync engine fails the affected item without retrying it.
    """


class RateLimited(TransientError):  # noqa: N818 - reads better than RateLimitedError
    """The upstream API rate-limited us (HTTP 429 / provider throttle).

    ``retry_after_s`` carries the upstream ``Retry-After`` hint when present so
    the backoff can honour the server's pacing instead of guessing.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AuthExpired(ConnectorError):  # noqa: N818 - a state, not a generic *Error
    """The stored credential is no longer valid and could not be refreshed.

    Distinct from a transient/permanent connector error because the resolution
    is *re-authorization by the user*, not a retry. The service marks the
    connection ``needs_reauth`` when it sees this.
    """


class WebhookVerificationError(IntegrationError):
    """An inbound webhook failed signature verification (reject, do not process)."""


__all__ = [
    "AuthExpired",
    "ConfigurationError",
    "ConnectorError",
    "IntegrationError",
    "PermanentError",
    "RateLimited",
    "TransientError",
    "WebhookVerificationError",
]
