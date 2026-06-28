"""The channel abstraction — one ``send`` per delivery surface.

A :class:`Channel` is the policy layer between the dispatcher and a transport: it
knows how to turn a :class:`Notification` (with its rendered message) into the
right transport call, what counts as "not reachable here" (a missing address →
permanent skip, not a retry), and returns a uniform :class:`ChannelOutcome` the
dispatcher records as delivery status.

Channels are thin and stateless; all the reliability machinery (retries, circuit
breaking, dead-letter) lives in the dispatcher / webhook engine so it is shared
across channels rather than reimplemented per channel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.notifications.deadletter import DeadLetterStore
from app.notifications.errors import PermanentTransportError, TransportError
from app.notifications.inapp import InAppNotification, InAppStore
from app.notifications.models import Channel, Notification
from app.notifications.transports import (
    EmailTransport,
    PushTransport,
)
from app.notifications.webhooks import (
    WebhookAttemptResult,
    WebhookDeliveryEngine,
    WebhookEndpoint,
)


class ChannelResult(StrEnum):
    """A channel send's coarse outcome (the dispatcher maps this to a status)."""

    DELIVERED = "delivered"
    #: Transient failure — the dispatcher should back off + retry.
    RETRYABLE = "retryable"
    #: Permanent failure — dead-letter without retrying.
    PERMANENT = "permanent"
    #: Not reachable on this channel (no address) — terminal skip.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ChannelOutcome:
    """The uniform result of a single channel send attempt."""

    result: ChannelResult
    provider_message_id: str | None = None
    error: str | None = None

    @property
    def delivered(self) -> bool:
        return self.result is ChannelResult.DELIVERED

    @property
    def retryable(self) -> bool:
        return self.result is ChannelResult.RETRYABLE


#: ``endpoints_for(user_id) -> list[WebhookEndpoint]`` — resolve a user's endpoints.
EndpointResolver = Callable[[str], Awaitable[list[WebhookEndpoint]]]


class NotificationChannel(Protocol):
    """A delivery surface with a single ``send``."""

    @property
    def kind(self) -> Channel: ...

    async def send(self, notification: Notification) -> ChannelOutcome: ...


class EmailChannel:
    """Delivers over an :class:`EmailTransport`."""

    kind = Channel.EMAIL

    def __init__(self, transport: EmailTransport) -> None:
        self._transport = transport

    async def send(self, notification: Notification) -> ChannelOutcome:
        address = notification.recipient.email
        if not address:
            return ChannelOutcome(ChannelResult.UNREACHABLE, error="no email address")
        if notification.message is None:
            return ChannelOutcome(ChannelResult.PERMANENT, error="message not rendered")
        try:
            result = await self._transport.send(address=address, message=notification.message)
        except TransportError as exc:
            return _outcome_from_transport_error(exc)
        return ChannelOutcome(
            ChannelResult.DELIVERED, provider_message_id=result.provider_message_id
        )


class PushChannel:
    """Delivers over a :class:`PushTransport`."""

    kind = Channel.PUSH

    def __init__(self, transport: PushTransport) -> None:
        self._transport = transport

    async def send(self, notification: Notification) -> ChannelOutcome:
        token = notification.recipient.push_token
        if not token:
            return ChannelOutcome(ChannelResult.UNREACHABLE, error="no push token")
        if notification.message is None:
            return ChannelOutcome(ChannelResult.PERMANENT, error="message not rendered")
        try:
            result = await self._transport.send(token=token, message=notification.message)
        except TransportError as exc:
            return _outcome_from_transport_error(exc)
        return ChannelOutcome(
            ChannelResult.DELIVERED, provider_message_id=result.provider_message_id
        )


class InAppChannel:
    """Persists to a user's in-app inbox (no transport / never fails transiently)."""

    kind = Channel.IN_APP

    def __init__(self, store: InAppStore) -> None:
        self._store = store

    async def send(self, notification: Notification) -> ChannelOutcome:
        if notification.message is None:
            return ChannelOutcome(ChannelResult.PERMANENT, error="message not rendered")
        await self._store.add(InAppNotification.from_notification(notification))
        return ChannelOutcome(ChannelResult.DELIVERED, provider_message_id=notification.id)


class WebhookChannel:
    """Delivers to every endpoint subscribed to the event via the signing engine.

    Webhooks fan out (a user may register several endpoints), so this channel
    resolves the recipient's endpoints from an injected lookup, then runs each
    through the :class:`WebhookDeliveryEngine`. The channel's outcome is the
    *aggregate*: delivered if any endpoint took it, retryable if any endpoint asked
    for a retry, permanent only if every endpoint permanently failed, unreachable
    if the user has no subscribed endpoints.
    """

    kind = Channel.WEBHOOK

    def __init__(
        self,
        engine: WebhookDeliveryEngine,
        *,
        endpoints_for: EndpointResolver,
        dead_letters: DeadLetterStore | None = None,
    ) -> None:
        self._engine = engine
        self._endpoints_for = endpoints_for
        self._dead_letters = dead_letters

    async def send(self, notification: Notification) -> ChannelOutcome:
        endpoints = await self._endpoints_for(notification.recipient.user_id)
        subscribed = [e for e in endpoints if e.wants(notification.event.value)]
        if not subscribed:
            return ChannelOutcome(ChannelResult.UNREACHABLE, error="no webhook endpoints")

        payload = _webhook_payload(notification)
        any_delivered = False
        any_retry = False
        last_error: str | None = None
        last_provider_id: str | None = None
        for endpoint in subscribed:
            attempt = await self._engine.deliver(
                endpoint,
                payload,
                event=notification.event.value,
                delivery_id=notification.id,
            )
            if attempt.result is WebhookAttemptResult.DELIVERED:
                any_delivered = True
                last_provider_id = attempt.provider_message_id
            elif attempt.result in (
                WebhookAttemptResult.RETRY,
                WebhookAttemptResult.CIRCUIT_OPEN,
            ):
                any_retry = True
                last_error = attempt.error
            elif attempt.result is WebhookAttemptResult.DEADLETTER:
                last_error = attempt.error
        if any_delivered:
            return ChannelOutcome(
                ChannelResult.DELIVERED, provider_message_id=last_provider_id, error=last_error
            )
        if any_retry:
            return ChannelOutcome(ChannelResult.RETRYABLE, error=last_error)
        return ChannelOutcome(ChannelResult.PERMANENT, error=last_error)


def _webhook_payload(notification: Notification) -> dict[str, object]:
    """The JSON envelope POSTed to a webhook endpoint."""
    return {
        "id": notification.id,
        "event": notification.event.value,
        "created_at": notification.created_at.isoformat(),
        "book_id": notification.book_id,
        "session_id": notification.session_id,
        "data": notification.data,
    }


def _outcome_from_transport_error(exc: TransportError) -> ChannelOutcome:
    if isinstance(exc, PermanentTransportError) or not exc.retryable:
        return ChannelOutcome(ChannelResult.PERMANENT, error=str(exc))
    return ChannelOutcome(ChannelResult.RETRYABLE, error=str(exc))


__all__ = [
    "ChannelOutcome",
    "ChannelResult",
    "EmailChannel",
    "EndpointResolver",
    "InAppChannel",
    "NotificationChannel",
    "PushChannel",
    "WebhookChannel",
]
