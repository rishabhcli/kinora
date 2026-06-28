"""``NotificationService`` — the facade the rest of Kinora talks to.

One object that wires the whole platform (EventRouter → preferences/quiet-hours/
digest gating → outbox → channels → retries/circuit/dead-letter → status), with
**sane in-memory defaults** so it is runnable with zero infrastructure and zero
credits (the hard constraint), and every collaborator overridable as a seam so
the composition root can swap in DB/Redis-backed stores and real transports.

The two entry points:

* :meth:`notify` — push a single :class:`DomainEventEnvelope` to one recipient,
  fanning it out across their opted-in channels. Returns the per-channel results.
* :meth:`emit` — convenience: build the envelope from an event + data + recipient
  and notify in one call (what the domain-event hooks use).

Plus operational sweeps: :meth:`flush_due_digests` (roll up + send digests) and
helpers to read the in-app inbox / delivery status / dead-letters that the API
route surfaces.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.notifications.backoff import RetryPolicy
from app.notifications.channels import (
    EmailChannel,
    EndpointResolver,
    InAppChannel,
    NotificationChannel,
    PushChannel,
    WebhookChannel,
)
from app.notifications.circuit import CircuitRegistry
from app.notifications.deadletter import (
    DeadLetter,
    DeadLetterStore,
    InMemoryDeadLetterStore,
)
from app.notifications.delivery import (
    DeliveryTracker,
    InMemoryDeliveryTracker,
)
from app.notifications.digest import (
    DigestAccumulator,
    InMemoryDigestAccumulator,
    build_digest_notification,
)
from app.notifications.dispatcher import Dispatcher, DispatchOutcome, DispatchResult
from app.notifications.events import DomainEvent, DomainEventEnvelope
from app.notifications.inapp import (
    InAppNotification,
    InAppStore,
    InMemoryInAppStore,
)
from app.notifications.models import (
    Channel,
    DeliveryRecord,
    Recipient,
)
from app.notifications.outbox import InMemoryOutbox, Outbox
from app.notifications.preferences import NotificationPreferences
from app.notifications.subscriptions import EventRouter
from app.notifications.templates import TemplateRegistry
from app.notifications.transports import (
    EmailTransport,
    LoggingEmailTransport,
    LoggingPushTransport,
    PushTransport,
    WebhookTransport,
)
from app.notifications.webhooks import WebhookDeliveryEngine, WebhookEndpoint

#: ``preferences_for(user_id) -> NotificationPreferences`` — load (or default) prefs.
PreferencesResolver = Callable[[str], Awaitable[NotificationPreferences]]


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """The aggregate result of a single :meth:`NotificationService.notify`."""

    event: DomainEvent
    results: list[DispatchResult]

    @property
    def delivered_channels(self) -> list[Channel]:
        return [r.channel for r in self.results if r.outcome is DispatchOutcome.DELIVERED]

    @property
    def any_delivered(self) -> bool:
        return any(r.outcome is DispatchOutcome.DELIVERED for r in self.results)


def _noop_endpoints(_user_id: str) -> Awaitable[list[WebhookEndpoint]]:
    async def _empty() -> list[WebhookEndpoint]:
        return []

    return _empty()


class NotificationService:
    """Wire + drive the notifications platform behind one handle."""

    def __init__(
        self,
        *,
        templates: TemplateRegistry | None = None,
        outbox: Outbox | None = None,
        tracker: DeliveryTracker | None = None,
        dead_letters: DeadLetterStore | None = None,
        digest: DigestAccumulator | None = None,
        inapp_store: InAppStore | None = None,
        email_transport: EmailTransport | None = None,
        push_transport: PushTransport | None = None,
        webhook_transport: WebhookTransport | None = None,
        endpoints_for: EndpointResolver | None = None,
        preferences_for: PreferencesResolver | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = lambda: datetime.now(UTC).timestamp(),
        log: Callable[..., None] = lambda *a, **k: None,
    ) -> None:
        self._templates = templates or TemplateRegistry()
        self._outbox = outbox or InMemoryOutbox()
        self._tracker = tracker or InMemoryDeliveryTracker()
        self._dead_letters = dead_letters or InMemoryDeadLetterStore()
        self._digest = digest or InMemoryDigestAccumulator()
        self._inapp = inapp_store or InMemoryInAppStore()
        self._router = EventRouter()
        self._retry = retry_policy or RetryPolicy()
        self._clock = clock
        self._log = log
        self._endpoints_for: EndpointResolver = endpoints_for or _noop_endpoints
        self._preferences_for = preferences_for

        # Webhook delivery engine (signing + circuit + retries) over the transport.
        webhook_engine = WebhookDeliveryEngine(
            webhook_transport or _NullWebhookTransport(),
            circuits=CircuitRegistry(),
            retry_policy=self._retry,
            clock=clock,
            log=log,
        )

        self._channels: dict[Channel, NotificationChannel] = {
            Channel.IN_APP: InAppChannel(self._inapp),
            Channel.EMAIL: EmailChannel(email_transport or LoggingEmailTransport(log=log)),
            Channel.PUSH: PushChannel(push_transport or LoggingPushTransport(log=log)),
            Channel.WEBHOOK: WebhookChannel(
                webhook_engine,
                endpoints_for=self._endpoints_for,
                dead_letters=self._dead_letters,
            ),
        }
        self._dispatcher = Dispatcher(
            channels=self._channels,
            templates=self._templates,
            outbox=self._outbox,
            tracker=self._tracker,
            dead_letters=self._dead_letters,
            digest=self._digest,
            retry_policy=self._retry,
            clock=clock,
            log=log,
        )

    # -- read-side accessors (for the API / tests) --------------------------- #

    @property
    def inapp(self) -> InAppStore:
        return self._inapp

    @property
    def tracker(self) -> DeliveryTracker:
        return self._tracker

    @property
    def dead_letters(self) -> DeadLetterStore:
        return self._dead_letters

    @property
    def templates(self) -> TemplateRegistry:
        return self._templates

    # -- the main entry points ----------------------------------------------- #

    async def notify(
        self,
        envelope: DomainEventEnvelope,
        *,
        recipient: Recipient,
        preferences: NotificationPreferences | None = None,
    ) -> NotifyResult:
        """Fan ``envelope`` out across the recipient's opted-in channels."""
        prefs = preferences or await self._resolve_prefs(recipient.user_id)
        notifications = self._router.route(envelope, recipient=recipient, preferences=prefs)
        results: list[DispatchResult] = []
        for notification in notifications:
            results.append(await self._dispatcher.dispatch(notification, preferences=prefs))
        # NB: ``event`` is structlog's reserved positional (the message), so log the
        # domain event under a distinct key to avoid a kwarg collision.
        self._log(
            "notifications.notify",
            event_name=envelope.event.value,
            user_id=recipient.user_id,
            channels=len(notifications),
        )
        return NotifyResult(event=envelope.event, results=results)

    async def emit(
        self,
        event: DomainEvent,
        *,
        recipient: Recipient,
        data: dict[str, Any] | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        dedup_key: str | None = None,
        preferences: NotificationPreferences | None = None,
    ) -> NotifyResult:
        """Build an envelope + notify in one call (the domain-event hook surface)."""
        envelope = DomainEventEnvelope(
            event=event,
            user_id=recipient.user_id,
            book_id=book_id,
            session_id=session_id,
            dedup_key=dedup_key,
            data=data or {},
        )
        return await self.notify(envelope, recipient=recipient, preferences=preferences)

    # -- operational sweeps -------------------------------------------------- #

    async def flush_due_digests(
        self, *, recipient_for: Callable[[str], Awaitable[Recipient]]
    ) -> list[NotifyResult]:
        """Roll up + send every digest whose interval has elapsed.

        ``recipient_for`` resolves a user's current address book (a digest may be
        sent long after the items accumulated). Each due bucket becomes one
        ``DIGEST_READY`` notification routed through the normal pipeline.
        """
        now = self._clock()
        results: list[NotifyResult] = []
        # The accumulator does not know each user's interval, so we resolve prefs
        # per pending user and flush only buckets whose own cadence has elapsed.
        pending = await self._digest.pending_user_ids()
        for user_id in pending:
            prefs = await self._resolve_prefs(user_id)
            if not prefs.digest.enabled:
                continue
            bucket = await self._digest.flush_if_due(
                user_id, now=now, interval_s=prefs.digest.interval_seconds
            )
            if bucket is None:
                continue
            recipient = await recipient_for(user_id)
            base = bucket.items[0].model_copy(update={"recipient": recipient})
            digest_notification = build_digest_notification(
                bucket, notification_id=f"ntf_{uuid.uuid4().hex[:16]}", base=base
            )
            envelope = DomainEventEnvelope(
                event=DomainEvent.DIGEST_READY,
                user_id=user_id,
                dedup_key=digest_notification.idempotency_key,
                data=digest_notification.data,
            )
            results.append(await self.notify(envelope, recipient=recipient, preferences=prefs))
        return results

    async def list_inbox(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[InAppNotification]:
        """The user's in-app inbox (durable counterpart to the SSE feed)."""
        return await self._inapp.list_for_user(user_id, limit=limit, unread_only=unread_only)

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        """Mark an in-app inbox item read."""
        return await self._inapp.mark_read(user_id, notification_id)

    async def unread_count(self, user_id: str) -> int:
        return await self._inapp.unread_count(user_id)

    async def list_deliveries(self, user_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        """Delivery-status records for the user (§12 status tracking)."""
        return await self._tracker.list_for_user(user_id, limit=limit)

    async def list_dead_letters(self, user_id: str, *, limit: int = 100) -> list[DeadLetter]:
        """Dead-lettered notifications for the user."""
        return await self._dead_letters.list_for_user(user_id, limit=limit)

    # -- internals ----------------------------------------------------------- #

    async def _resolve_prefs(self, user_id: str) -> NotificationPreferences:
        if self._preferences_for is not None:
            return await self._preferences_for(user_id)
        return NotificationPreferences.defaults(user_id)


class _NullWebhookTransport:
    """A webhook transport that refuses to send (used when none is injected).

    With no real HTTP client wired, attempting a webhook is a *permanent* failure
    (it dead-letters rather than retrying forever). Production injects a real HTTP
    transport; tests inject ``RecordingWebhookTransport``.
    """

    async def send(self, *, url: str, body: bytes, headers: dict[str, str]) -> Any:
        from app.notifications.errors import PermanentTransportError

        raise PermanentTransportError("no webhook transport configured")


__all__ = ["NotificationService", "NotifyResult", "PreferencesResolver"]
