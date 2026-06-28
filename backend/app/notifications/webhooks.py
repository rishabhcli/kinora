"""Outbound webhook signing + delivery (kinora.md §12 reliability).

Outbound webhooks let a third-party integration (a Slack relay, a user's own
server) receive Kinora domain events. Because the receiver must trust the payload
came from us and was not replayed, each POST is **HMAC-SHA256 signed** over a
``"{timestamp}.{body}"`` string (the Stripe/Svix scheme): the signature proves
authenticity *and* the timestamp lets the receiver reject replays outside a
tolerance window.

* :class:`WebhookSigner` — pure signing + constant-time verification.
* :class:`WebhookEndpoint` — a registered destination (url, secret, event filter).
* :class:`WebhookDeliveryEngine` — wraps a :class:`WebhookTransport` with the
  §12 reliability stack: a per-endpoint :class:`CircuitBreaker`, exponential
  backoff retries, and a dead-letter handoff when retries are exhausted. It does
  *not* sleep between retries itself — it returns a :class:`DeliveryAttempt`
  telling the caller (the dispatcher / a worker) when to re-attempt, so the retry
  schedule is durable and testable rather than blocking a coroutine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.notifications.backoff import RetryDecision, RetryPolicy, RetryState
from app.notifications.circuit import CircuitRegistry
from app.notifications.errors import TransportError
from app.notifications.transports import WebhookTransport

#: Header names on every outbound webhook (lowercase by HTTP convention).
SIGNATURE_HEADER = "x-kinora-signature"
TIMESTAMP_HEADER = "x-kinora-timestamp"
EVENT_HEADER = "x-kinora-event"
DELIVERY_HEADER = "x-kinora-delivery-id"

#: Default signature algorithm tag (versioned so we can rotate without breaking).
SIGNATURE_VERSION = "v1"


class WebhookSigner:
    """HMAC-SHA256 signer for outbound webhooks (replay-safe via a timestamp).

    The signed string is ``"{timestamp}.{body}"``; the header value is
    ``"v1=<hex>"`` so the version is explicit and rotatable. Verification is
    constant-time and enforces a timestamp tolerance to reject replays.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def sign(self, secret: str, body: bytes, *, timestamp: int | None = None) -> tuple[str, int]:
        """Return ``(signature_header_value, timestamp)`` for ``body`` under ``secret``."""
        ts = int(self._clock()) if timestamp is None else timestamp
        digest = self._digest(secret, body, ts)
        return f"{SIGNATURE_VERSION}={digest}", ts

    def verify(
        self,
        secret: str,
        body: bytes,
        *,
        signature: str,
        timestamp: int,
        tolerance_s: int = 300,
    ) -> bool:
        """Constant-time verify a signature, rejecting replays past ``tolerance_s``."""
        if abs(int(self._clock()) - int(timestamp)) > tolerance_s:
            return False
        expected = f"{SIGNATURE_VERSION}={self._digest(secret, body, int(timestamp))}"
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _digest(secret: str, body: bytes, timestamp: int) -> str:
        signed = f"{timestamp}.".encode() + body
        return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def generate_webhook_secret() -> str:
    """Mint a fresh high-entropy webhook signing secret."""
    import secrets

    return f"whsec_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    """A registered outbound webhook destination."""

    id: str
    user_id: str
    url: str
    secret: str
    #: The set of ``DomainEvent`` values this endpoint wants ("*" = all).
    events: frozenset[str] = field(default_factory=frozenset)
    active: bool = True

    def wants(self, event: str) -> bool:
        """Whether this endpoint is subscribed to ``event``."""
        if not self.active:
            return False
        return "*" in self.events or event in self.events


class WebhookAttemptResult(StrEnum):
    """The outcome of a single delivery attempt by the engine."""

    DELIVERED = "delivered"
    RETRY = "retry"
    DEADLETTER = "deadletter"
    CIRCUIT_OPEN = "circuit_open"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """The result of :meth:`WebhookDeliveryEngine.deliver` for one attempt set.

    ``retry_at`` is an epoch second the caller should re-attempt at (for ``RETRY``
    / ``CIRCUIT_OPEN``); ``provider_message_id`` is set on ``DELIVERED``.
    """

    result: WebhookAttemptResult
    attempts: int
    retry_at: float | None = None
    provider_message_id: str | None = None
    error: str | None = None


class WebhookDeliveryEngine:
    """Deliver a signed payload to an endpoint with §12 reliability guarantees.

    Composes the transport with a circuit breaker (one per endpoint id) and the
    retry policy. The engine performs *one* network attempt per :meth:`deliver`
    call and reports what to do next — looping/sleeping is the caller's job so the
    schedule survives a process restart (durable retries).
    """

    def __init__(
        self,
        transport: WebhookTransport,
        *,
        signer: WebhookSigner | None = None,
        circuits: CircuitRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[..., None] = lambda *a, **k: None,
    ) -> None:
        self._transport = transport
        self._signer = signer or WebhookSigner(clock=clock)
        self._circuits = circuits or CircuitRegistry()
        self._retry = retry_policy or RetryPolicy()
        self._clock = clock
        self._log = log

    def build_headers(
        self, endpoint: WebhookEndpoint, body: bytes, *, event: str, delivery_id: str
    ) -> dict[str, str]:
        """The full signed header set for a POST to ``endpoint``."""
        signature, ts = self._signer.sign(endpoint.secret, body)
        return {
            "content-type": "application/json",
            SIGNATURE_HEADER: signature,
            TIMESTAMP_HEADER: str(ts),
            EVENT_HEADER: event,
            DELIVERY_HEADER: delivery_id,
        }

    async def deliver(
        self,
        endpoint: WebhookEndpoint,
        payload: dict[str, object],
        *,
        event: str,
        delivery_id: str,
        state: RetryState | None = None,
    ) -> DeliveryAttempt:
        """Attempt one signed POST; report delivered / retry / deadletter / circuit-open."""
        state = state or RetryState()
        if not endpoint.wants(event):
            return DeliveryAttempt(result=WebhookAttemptResult.SKIPPED, attempts=state.attempts)

        breaker = self._circuits.get(endpoint.id)
        if not breaker.allow():
            retry_after = breaker.retry_after_s() or self._retry.base_s
            self._log("notifications.webhook.circuit_open", endpoint=endpoint.id)
            return DeliveryAttempt(
                result=WebhookAttemptResult.CIRCUIT_OPEN,
                attempts=state.attempts,
                retry_at=self._clock() + retry_after,
                error="circuit open",
            )

        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = self.build_headers(endpoint, body, event=event, delivery_id=delivery_id)
        try:
            result = await self._transport.send(url=endpoint.url, body=body, headers=headers)
        except TransportError as exc:
            return self._on_failure(endpoint, breaker, state, exc)
        except Exception as exc:  # noqa: BLE001 - unknown errors treated as transient
            return self._on_failure(
                endpoint, breaker, state, TransportError(str(exc), retryable=True)
            )

        breaker.record_success()
        self._log("notifications.webhook.delivered", endpoint=endpoint.id, attempts=state.attempts)
        return DeliveryAttempt(
            result=WebhookAttemptResult.DELIVERED,
            attempts=state.attempts,
            provider_message_id=result.provider_message_id,
        )

    def _on_failure(
        self,
        endpoint: WebhookEndpoint,
        breaker: object,
        state: RetryState,
        exc: TransportError,
    ) -> DeliveryAttempt:
        attempts = state.record_failure(str(exc))
        breaker.record_failure()  # type: ignore[attr-defined]
        # A permanent failure (4xx / bad address) is never retried — dead-letter now.
        if not exc.retryable:
            self._log(
                "notifications.webhook.permanent_fail", endpoint=endpoint.id, error=str(exc)
            )
            return DeliveryAttempt(
                result=WebhookAttemptResult.DEADLETTER, attempts=attempts, error=str(exc)
            )
        if self._retry.decide(attempts) is RetryDecision.DEADLETTER:
            self._log(
                "notifications.webhook.deadletter", endpoint=endpoint.id, attempts=attempts
            )
            return DeliveryAttempt(
                result=WebhookAttemptResult.DEADLETTER, attempts=attempts, error=str(exc)
            )
        delay = self._retry.delay_for(attempts)
        self._log(
            "notifications.webhook.retry",
            endpoint=endpoint.id,
            attempts=attempts,
            delay_s=round(delay, 2),
        )
        return DeliveryAttempt(
            result=WebhookAttemptResult.RETRY,
            attempts=attempts,
            retry_at=self._clock() + delay,
            error=str(exc),
        )


__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "DeliveryAttempt",
    "WebhookAttemptResult",
    "WebhookDeliveryEngine",
    "WebhookEndpoint",
    "WebhookSigner",
    "generate_webhook_secret",
]
