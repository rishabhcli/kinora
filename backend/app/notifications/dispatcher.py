"""The dispatcher — orchestrates one notification from intent to outcome.

Given a :class:`~app.notifications.models.Notification` (already routed to a
channel by the EventRouter), the dispatcher runs the full pipeline:

1. **Outbox claim** — idempotent: a duplicate of the same logical delivery is a
   no-op (§12.1).
2. **Quiet-hours gate** — non-urgent notifications inside a quiet window are
   *deferred* (status DEFERRED, ``not_before`` = window open), not dropped.
3. **Digest gate** — digestable notifications for a digest-enabled user are
   *accumulated* (status DEFERRED) and flushed later as a rollup.
4. **Render** — resolve + interpolate the localized template just-in-time.
5. **Channel send** — one attempt via the channel.
6. **Outcome handling** — delivered (status DELIVERED + in-app/tracker record),
   transient failure (status RETRYING + a ``retry_at``), permanent / cap reached
   (status DEADLETTERED + a dead-letter row). The channel itself owns webhook
   retries/circuit; the dispatcher owns email/push retries via the policy.

Like the webhook engine, the dispatcher performs *one* attempt per call and
reports when to retry (``DispatchResult.retry_at``) rather than sleeping, so the
retry schedule is durable and the whole thing is deterministic in tests with an
injected clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.notifications import metrics
from app.notifications.backoff import RetryDecision, RetryPolicy
from app.notifications.channels import ChannelResult, NotificationChannel
from app.notifications.deadletter import DeadLetter, DeadLetterStore
from app.notifications.delivery import DeliveryTracker, new_record, update_record
from app.notifications.digest import DigestAccumulator
from app.notifications.events import DomainEvent
from app.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationPriority,
)
from app.notifications.outbox import Outbox
from app.notifications.preferences import NotificationPreferences
from app.notifications.quiet_hours import next_open_at
from app.notifications.templates import TemplateRegistry


class DispatchOutcome(StrEnum):
    """The coarse result of a single dispatch attempt."""

    DELIVERED = "delivered"
    DEFERRED = "deferred"
    SUPPRESSED = "suppressed"
    RETRY = "retry"
    DEADLETTERED = "deadlettered"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What :meth:`Dispatcher.dispatch` did with a notification."""

    outcome: DispatchOutcome
    notification_id: str
    channel: Channel
    attempts: int = 0
    retry_at: float | None = None
    error: str | None = None


class Dispatcher:
    """Orchestrate a single notification through gating, send, retry, dead-letter."""

    def __init__(
        self,
        *,
        channels: dict[Channel, NotificationChannel],
        templates: TemplateRegistry,
        outbox: Outbox,
        tracker: DeliveryTracker,
        dead_letters: DeadLetterStore,
        digest: DigestAccumulator | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = lambda: datetime.now(UTC).timestamp(),
        log: Callable[..., None] = lambda *a, **k: None,
    ) -> None:
        self._channels = channels
        self._templates = templates
        self._outbox = outbox
        self._tracker = tracker
        self._dead_letters = dead_letters
        self._digest = digest
        self._retry = retry_policy or RetryPolicy()
        self._clock = clock
        self._log = log

    async def dispatch(
        self,
        notification: Notification,
        *,
        preferences: NotificationPreferences,
    ) -> DispatchResult:
        """Run one notification through the pipeline; report the outcome."""
        result = await self._dispatch(notification, preferences=preferences)
        metrics.inc_dispatched(notification.channel.value, result.outcome.value)
        return result

    async def _dispatch(
        self,
        notification: Notification,
        *,
        preferences: NotificationPreferences,
    ) -> DispatchResult:
        # 1. Idempotent outbox claim.
        entry = await self._outbox.claim(notification)
        if entry is None:
            self._log("notifications.dispatch.duplicate", key=notification.outbox_key())
            return DispatchResult(
                outcome=DispatchOutcome.DUPLICATE,
                notification_id=notification.id,
                channel=notification.channel,
            )

        now = self._clock()
        now_dt = datetime.fromtimestamp(now, tz=UTC)

        # 2. Quiet-hours gate (urgent bypasses).
        if not notification.priority.bypasses_quiet_hours and self._is_quiet(preferences, now_dt):
            open_at = next_open_at(preferences.quiet_hours, now_dt)
            return await self._defer(notification, not_before=open_at.timestamp())

        # 3. Digest gate (only digestable priorities for a digest-enabled user; in-app
        #    is always immediate so the inbox stays live; a DIGEST_READY rollup is the
        #    flush itself and must never be re-digested or it would never be sent).
        if (
            self._digest is not None
            and preferences.digest.enabled
            and notification.priority.digestable
            and notification.channel is not Channel.IN_APP
            and notification.event is not DomainEvent.DIGEST_READY
        ):
            await self._digest.add(notification, now=now)
            await self._set_status(notification, DeliveryStatus.DEFERRED)
            self._log("notifications.dispatch.digested", id=notification.id)
            return DispatchResult(
                outcome=DispatchOutcome.DEFERRED,
                notification_id=notification.id,
                channel=notification.channel,
            )

        # 4. Render the localized template just-in-time.
        rendered = self._templates.render(
            notification.event,
            notification.channel,
            locale=notification.recipient.locale or preferences.locale,
            data=notification.data,
        )
        notification = notification.model_copy(update={"message": rendered})

        # 5/6. Send + handle outcome.
        return await self._attempt(notification, attempt_no=entry.attempts + 1)

    async def _attempt(self, notification: Notification, *, attempt_no: int) -> DispatchResult:
        channel = self._channels.get(notification.channel)
        if channel is None:
            await self._set_status(notification, DeliveryStatus.SUPPRESSED, last_error="no channel")
            return DispatchResult(
                outcome=DispatchOutcome.SUPPRESSED,
                notification_id=notification.id,
                channel=notification.channel,
                error="channel not configured",
            )

        await self._set_status(notification, DeliveryStatus.SENDING, attempts=attempt_no)
        outcome = await channel.send(notification)

        if outcome.result is ChannelResult.DELIVERED:
            await self._set_status(
                notification,
                DeliveryStatus.DELIVERED,
                attempts=attempt_no,
                provider_message_id=outcome.provider_message_id,
            )
            self._log("notifications.dispatch.delivered", id=notification.id, attempt=attempt_no)
            return DispatchResult(
                outcome=DispatchOutcome.DELIVERED,
                notification_id=notification.id,
                channel=notification.channel,
                attempts=attempt_no,
            )

        if outcome.result is ChannelResult.UNREACHABLE:
            await self._set_status(
                notification,
                DeliveryStatus.SUPPRESSED,
                attempts=attempt_no,
                last_error=outcome.error,
            )
            return DispatchResult(
                outcome=DispatchOutcome.SUPPRESSED,
                notification_id=notification.id,
                channel=notification.channel,
                attempts=attempt_no,
                error=outcome.error,
            )

        # Permanent failure, or transient failure past the retry cap → dead-letter.
        cap_reached = self._retry.decide(attempt_no) is RetryDecision.DEADLETTER
        if outcome.result is ChannelResult.PERMANENT or cap_reached:
            await self._dead_letter(notification, attempts=attempt_no, error=outcome.error)
            return DispatchResult(
                outcome=DispatchOutcome.DEADLETTERED,
                notification_id=notification.id,
                channel=notification.channel,
                attempts=attempt_no,
                error=outcome.error,
            )

        # Transient failure within the cap → schedule a retry.
        delay = self._retry.delay_for(attempt_no)
        retry_at = self._clock() + delay
        await self._set_status(
            notification,
            DeliveryStatus.RETRYING,
            attempts=attempt_no,
            last_error=outcome.error,
            not_before=datetime.fromtimestamp(retry_at, tz=UTC),
        )
        self._log(
            "notifications.dispatch.retry",
            id=notification.id,
            attempt=attempt_no,
            delay_s=round(delay, 2),
        )
        return DispatchResult(
            outcome=DispatchOutcome.RETRY,
            notification_id=notification.id,
            channel=notification.channel,
            attempts=attempt_no,
            retry_at=retry_at,
            error=outcome.error,
        )

    async def retry(
        self, notification: Notification, *, preferences: NotificationPreferences
    ) -> DispatchResult:
        """Re-attempt a previously-RETRYING notification (caller honours ``retry_at``)."""
        entry = await self._outbox.get(notification.outbox_key())
        attempt_no = (entry.attempts if entry else 0) + 1
        if notification.message is None:
            rendered = self._templates.render(
                notification.event,
                notification.channel,
                locale=notification.recipient.locale or preferences.locale,
                data=notification.data,
            )
            notification = notification.model_copy(update={"message": rendered})
        return await self._attempt(notification, attempt_no=attempt_no)

    # -- internals ----------------------------------------------------------- #

    def _is_quiet(self, preferences: NotificationPreferences, now: datetime) -> bool:
        window = preferences.quiet_hours
        return bool(window and window.is_quiet(now))

    async def _defer(self, notification: Notification, *, not_before: float) -> DispatchResult:
        await self._set_status(
            notification,
            DeliveryStatus.DEFERRED,
            not_before=datetime.fromtimestamp(not_before, tz=UTC),
        )
        self._log("notifications.dispatch.deferred", id=notification.id, not_before=not_before)
        return DispatchResult(
            outcome=DispatchOutcome.DEFERRED,
            notification_id=notification.id,
            channel=notification.channel,
            retry_at=not_before,
        )

    async def _dead_letter(
        self, notification: Notification, *, attempts: int, error: str | None
    ) -> None:
        await self._set_status(
            notification, DeliveryStatus.DEADLETTERED, attempts=attempts, last_error=error
        )
        await self._dead_letters.add(
            DeadLetter.from_notification(
                notification,
                attempts=attempts,
                last_error=error,
                dead_letter_id=f"dlq_{uuid.uuid4().hex[:16]}",
            )
        )
        metrics.inc_deadletter(notification.channel.value)
        self._log(
            "notifications.dispatch.deadletter",
            id=notification.id,
            attempts=attempts,
            error=error,
        )

    async def _set_status(
        self,
        notification: Notification,
        status: DeliveryStatus,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        not_before: datetime | None = None,
        provider_message_id: str | None = None,
    ) -> None:
        # Outbox status (idempotent unit) …
        await self._outbox.update_status(
            notification.outbox_key(),
            status,
            attempts=attempts,
            last_error=last_error,
            not_before=not_before.timestamp() if not_before else None,
        )
        # … and the finer-grained per-notification delivery record.
        existing = await self._tracker.get(notification.id)
        record = existing or new_record(notification, status=status)
        update_record(
            record,
            status=status,
            attempts=attempts,
            last_error=last_error,
            not_before=not_before,
            provider_message_id=provider_message_id,
        )
        await self._tracker.record(record)


__all__ = ["DispatchOutcome", "DispatchResult", "Dispatcher", "NotificationPriority"]
