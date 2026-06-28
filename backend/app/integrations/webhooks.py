"""Webhook receivers for push-based sources + HMAC signature verification.

Some sources can *push* "you have new material" notifications (a Readwise
trigger, a custom RSS-hub callback). Rather than each connector inventing its own
endpoint, the framework exposes one receiver: a provider posts to
``/integrations/webhooks/{provider}`` and, if the payload's signature verifies,
the matching connection is queued for an incremental sync.

The only security-critical part is verifying the payload actually came from the
provider — done with a constant-time HMAC comparison against a per-provider
shared secret (:class:`WebhookVerifier`). An unverified payload is rejected
before any work happens. The verifier is deliberately small and standalone so it
can be unit-tested without HTTP.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from app.core.logging import get_logger
from app.integrations.errors import WebhookVerificationError

logger = get_logger("app.integrations.webhooks")


@dataclass(frozen=True)
class WebhookSecret:
    """One provider's webhook signing secret + how it signs."""

    provider: str
    secret: str
    #: The header carrying the signature ("X-Hub-Signature-256", etc.).
    signature_header: str = "x-signature"
    #: The digest algorithm name passed to :mod:`hmac` ("sha256"/"sha1").
    algorithm: str = "sha256"
    #: A prefix the provider prepends to the hex digest ("sha256=").
    signature_prefix: str = ""


class WebhookVerifier:
    """Verify an inbound webhook's HMAC signature in constant time."""

    def __init__(self, secrets: dict[str, WebhookSecret] | None = None) -> None:
        self._secrets: dict[str, WebhookSecret] = dict(secrets or {})

    def register(self, secret: WebhookSecret) -> WebhookVerifier:
        """Register a provider's signing secret."""
        self._secrets[secret.provider] = secret
        return self

    def is_configured(self, provider: str) -> bool:
        """Whether a signing secret is registered for ``provider``."""
        return provider in self._secrets

    def expected_signature(self, provider: str, body: bytes) -> str:
        """Compute the expected signature for ``body`` under ``provider``'s secret."""
        secret = self._require(provider)
        digest = hmac.new(
            secret.secret.encode("utf-8"), body, getattr(hashlib, secret.algorithm)
        ).hexdigest()
        return f"{secret.signature_prefix}{digest}"

    def verify(self, provider: str, body: bytes, headers: dict[str, str]) -> None:
        """Verify the request, raising :class:`WebhookVerificationError` on failure.

        Args:
            provider: the connector name from the URL.
            body: the **raw** request body bytes (sign-over content).
            headers: the request headers (case-insensitive lookup).
        """
        secret = self._require(provider)
        lowered = {k.lower(): v for k, v in headers.items()}
        provided = lowered.get(secret.signature_header.lower())
        if not provided:
            raise WebhookVerificationError(
                f"missing signature header {secret.signature_header!r} for {provider}"
            )
        expected = self.expected_signature(provider, body)
        if not hmac.compare_digest(provided.strip(), expected):
            raise WebhookVerificationError(f"signature mismatch for {provider}")

    def _require(self, provider: str) -> WebhookSecret:
        secret = self._secrets.get(provider)
        if secret is None:
            raise WebhookVerificationError(f"no webhook secret registered for {provider!r}")
        return secret


@dataclass(frozen=True)
class WebhookEvent:
    """A verified inbound webhook, ready to drive a sync."""

    provider: str
    body: bytes
    #: Provider-specific connection hint pulled from the payload (account id),
    #: when the receiver can extract it; otherwise the service resolves the
    #: connection by ``(user, provider)``.
    connection_hint: str | None = None


__all__ = ["WebhookEvent", "WebhookSecret", "WebhookVerifier"]
