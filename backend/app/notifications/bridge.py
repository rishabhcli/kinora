"""Bridge the live §5.6 event bus into durable notifications.

The scheduler / render pipeline / ingest already publish ephemeral §5.6 events to
Redis pub/sub channels for the live UI (``clip_ready``, ``budget_low``,
``conflict_choice``, and the ``ingest_progress`` ``stage: ready`` completion).
This bridge is a **consumer** of those same channels — it never touches the
publishers (so it stays strictly additive) — that maps each notifiable wire event
onto a :class:`~app.notifications.events.DomainEventEnvelope` and hands it to the
:class:`~app.notifications.service.NotificationService` for durable, out-of-band
delivery.

It runs as one background task per process (the API can ``spawn`` it). The user a
notification is for is resolved from book/session ownership via injected lookups,
so the bridge owns no DB queries itself.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from app.notifications.events import DomainEvent, DomainEventEnvelope, from_session_event
from app.notifications.models import Recipient
from app.notifications.service import NotificationService

#: ``recipient_for_book(book_id) -> Recipient | None`` — owner address book.
RecipientForBook = Callable[[str], Awaitable["Recipient | None"]]
#: ``recipient_for_session(session_id) -> Recipient | None``.
RecipientForSession = Callable[[str], Awaitable["Recipient | None"]]


def book_ready_envelope(message: dict[str, Any]) -> DomainEventEnvelope | None:
    """Map an ``ingest_progress`` completion into a ``BOOK_READY`` envelope.

    Ingest publishes ``{event: ingest_progress, stage: ready, pct: 1.0, book_id}``
    on the library channel when a book finishes Phase A; that is the durable
    "your book is ready" signal (other progress ticks are live-UI only).
    """
    if message.get("event") != "ingest_progress":
        return None
    if str(message.get("stage")) != "ready":
        return None
    book_id = message.get("book_id")
    if not book_id:
        return None
    return DomainEventEnvelope(
        event=DomainEvent.BOOK_READY,
        book_id=str(book_id),
        dedup_key=str(book_id),
        data={k: v for k, v in message.items() if k not in {"event", "stage", "pct"}},
    )


class NotificationBridge:
    """Subscribe to live event channels and emit durable notifications."""

    def __init__(
        self,
        redis: Any,
        service: NotificationService,
        *,
        recipient_for_book: RecipientForBook,
        recipient_for_session: RecipientForSession,
        title_for_book: Callable[[str], Awaitable[str | None]] | None = None,
        log: Callable[..., None] = lambda *a, **k: None,
    ) -> None:
        self._redis = redis
        self._service = service
        self._recipient_for_book = recipient_for_book
        self._recipient_for_session = recipient_for_session
        self._title_for_book = title_for_book
        self._log = log

    async def handle(self, message: dict[str, Any]) -> bool:
        """Route one wire event into a notification; return whether one was emitted.

        Resolves the recipient from the message's session/book scope, maps the
        wire event to a domain envelope (book-ready from ``ingest_progress``,
        otherwise via :func:`from_session_event`), and emits. Best-effort: a
        resolution miss or a delivery error is logged, never raised, so the bridge
        loop is resilient.
        """
        try:
            envelope = book_ready_envelope(message) or from_session_event(message)
            if envelope is None:
                return False
            recipient = await self._resolve_recipient(envelope)
            if recipient is None:
                self._log("notifications.bridge.no_recipient", event=envelope.event.value)
                return False
            envelope = await self._enrich(envelope)
            await self._service.notify(envelope, recipient=recipient)
            return True
        except Exception as exc:  # noqa: BLE001 - the bridge must never crash on one event
            self._log("notifications.bridge.error", error=str(exc))
            return False

    async def run(self, *channels: str, poll_timeout: float = 5.0) -> None:
        """Subscribe to ``channels`` and route events until cancelled."""
        async with self._redis.subscribe(*channels) as pubsub:
            self._log("notifications.bridge.started", channels=list(channels))
            with contextlib.suppress(Exception):
                while True:
                    message = await self._redis.next_message(pubsub, timeout=poll_timeout)
                    if isinstance(message, dict):
                        await self.handle(message)

    async def run_pattern(
        self, pattern: str = "kinora:events:*", *, poll_timeout: float = 5.0
    ) -> None:
        """Pattern-subscribe to every event channel and route until cancelled.

        Uses ``psubscribe`` on the raw client so one bridge task covers all
        per-session/per-book/per-user channels without enumerating them. JSON
        decoding mirrors :meth:`app.redis.client.RedisClient.next_message`.
        """
        import json

        raw = getattr(self._redis, "raw", self._redis)
        pubsub = raw.pubsub()
        await pubsub.psubscribe(pattern)
        self._log("notifications.bridge.started", pattern=pattern)
        try:
            with contextlib.suppress(Exception):
                while True:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=poll_timeout
                    )
                    if msg is not None and msg.get("type") == "pmessage":
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            await self.handle(json.loads(msg["data"]))
        finally:
            with contextlib.suppress(Exception):
                await pubsub.punsubscribe(pattern)
                await pubsub.aclose()

    async def _enrich(self, envelope: DomainEventEnvelope) -> DomainEventEnvelope:
        """Fill in the book ``title`` for the template when the wire event omits it."""
        if self._title_for_book is None or envelope.book_id is None:
            return envelope
        if envelope.data.get("title"):
            return envelope
        title = await self._title_for_book(envelope.book_id)
        if not title:
            return envelope
        return envelope.model_copy(update={"data": {**envelope.data, "title": title}})

    async def _resolve_recipient(self, envelope: DomainEventEnvelope) -> Recipient | None:
        if envelope.session_id:
            recipient = await self._recipient_for_session(envelope.session_id)
            if recipient is not None:
                return recipient
        if envelope.book_id:
            return await self._recipient_for_book(envelope.book_id)
        return None


__all__ = ["NotificationBridge", "book_ready_envelope"]
