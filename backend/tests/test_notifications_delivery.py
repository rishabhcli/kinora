"""Delivery-engine tests: dispatcher, webhook signing/retries/circuit, the service.

All in-memory + fake transports — no network, no infra. The injected ``FakeClock``
makes retry/quiet-hours timing deterministic.
"""

from __future__ import annotations

import json

import pytest

from app.notifications.backoff import RetryPolicy, RetryState
from app.notifications.channels import (
    ChannelResult,
    EmailChannel,
    InAppChannel,
)
from app.notifications.deadletter import InMemoryDeadLetterStore
from app.notifications.delivery import InMemoryDeliveryTracker
from app.notifications.digest import InMemoryDigestAccumulator
from app.notifications.dispatcher import Dispatcher, DispatchOutcome
from app.notifications.events import DomainEvent, DomainEventEnvelope
from app.notifications.inapp import InMemoryInAppStore
from app.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationPriority,
    Recipient,
)
from app.notifications.outbox import InMemoryOutbox
from app.notifications.preferences import NotificationPreferences
from app.notifications.service import NotificationService
from app.notifications.templates import TemplateRegistry
from app.notifications.transports import (
    InMemoryEmailTransport,
    RecordingWebhookTransport,
)
from app.notifications.webhooks import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookAttemptResult,
    WebhookDeliveryEngine,
    WebhookEndpoint,
    WebhookSigner,
    generate_webhook_secret,
)


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _notification(
    *,
    channel: Channel = Channel.EMAIL,
    event: DomainEvent = DomainEvent.BOOK_READY,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    email: str | None = "reader@example.com",
    locale: str = "en",
) -> Notification:
    return Notification(
        id=f"ntf_{channel.value}_{event.value}",
        event=event,
        channel=channel,
        recipient=Recipient(user_id="u1", email=email, locale=locale),
        priority=priority,
        idempotency_key=f"{event.value}:once",
        data={"title": "Moby-Dick", "remaining_s": 12},
    )


def _dispatcher(
    *,
    email_transport: InMemoryEmailTransport | None = None,
    clock: FakeClock | None = None,
    retry_policy: RetryPolicy | None = None,
    inapp: InMemoryInAppStore | None = None,
    outbox: InMemoryOutbox | None = None,
    tracker: InMemoryDeliveryTracker | None = None,
    dead_letters: InMemoryDeadLetterStore | None = None,
    digest: InMemoryDigestAccumulator | None = None,
) -> tuple[Dispatcher, dict[str, object]]:
    clock = clock or FakeClock()
    email_transport = email_transport or InMemoryEmailTransport()
    inapp = inapp or InMemoryInAppStore()
    outbox = outbox or InMemoryOutbox()
    tracker = tracker or InMemoryDeliveryTracker()
    dead_letters = dead_letters or InMemoryDeadLetterStore()
    channels = {
        Channel.EMAIL: EmailChannel(email_transport),
        Channel.IN_APP: InAppChannel(inapp),
    }
    dispatcher = Dispatcher(
        channels=channels,  # type: ignore[arg-type]
        templates=TemplateRegistry(),
        outbox=outbox,
        tracker=tracker,
        dead_letters=dead_letters,
        digest=digest,
        retry_policy=retry_policy or RetryPolicy(max_attempts=3, base_s=2.0, jitter=False),
        clock=clock,
    )
    return dispatcher, {
        "email": email_transport,
        "inapp": inapp,
        "outbox": outbox,
        "tracker": tracker,
        "dead_letters": dead_letters,
        "clock": clock,
    }


# --------------------------------------------------------------------------- #
# dispatcher: happy path + idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_delivers_and_renders() -> None:
    dispatcher, deps = _dispatcher()
    prefs = NotificationPreferences.defaults("u1")
    result = await dispatcher.dispatch(_notification(), preferences=prefs)
    assert result.outcome is DispatchOutcome.DELIVERED
    email: InMemoryEmailTransport = deps["email"]  # type: ignore[assignment]
    assert len(email.sent) == 1
    assert "Moby-Dick" in email.sent[0].message.subject


@pytest.mark.asyncio
async def test_dispatch_is_idempotent() -> None:
    dispatcher, deps = _dispatcher()
    prefs = NotificationPreferences.defaults("u1")
    first = await dispatcher.dispatch(_notification(), preferences=prefs)
    second = await dispatcher.dispatch(_notification(), preferences=prefs)
    assert first.outcome is DispatchOutcome.DELIVERED
    assert second.outcome is DispatchOutcome.DUPLICATE
    email: InMemoryEmailTransport = deps["email"]  # type: ignore[assignment]
    assert len(email.sent) == 1  # only sent once


@pytest.mark.asyncio
async def test_dispatch_unreachable_when_no_address() -> None:
    dispatcher, _ = _dispatcher()
    prefs = NotificationPreferences.defaults("u1")
    result = await dispatcher.dispatch(_notification(email=None), preferences=prefs)
    assert result.outcome is DispatchOutcome.SUPPRESSED


# --------------------------------------------------------------------------- #
# dispatcher: retries + dead-letter
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_retries_transient_then_succeeds() -> None:
    transport = InMemoryEmailTransport(fail_times=1)  # first attempt fails transiently
    dispatcher, deps = _dispatcher(email_transport=transport)
    prefs = NotificationPreferences.defaults("u1")
    first = await dispatcher.dispatch(_notification(), preferences=prefs)
    assert first.outcome is DispatchOutcome.RETRY
    assert first.retry_at is not None
    # The dispatcher reports retry_at; caller re-invokes after the delay.
    notification = _notification()
    notification = notification.model_copy(update={"message": None})
    second = await dispatcher.retry(notification, preferences=prefs)
    assert second.outcome is DispatchOutcome.DELIVERED
    email: InMemoryEmailTransport = deps["email"]  # type: ignore[assignment]
    assert len(email.sent) == 1


@pytest.mark.asyncio
async def test_dispatch_permanent_failure_dead_letters_immediately() -> None:
    transport = InMemoryEmailTransport(always_fail=True, permanent=True)
    dispatcher, deps = _dispatcher(email_transport=transport)
    prefs = NotificationPreferences.defaults("u1")
    result = await dispatcher.dispatch(_notification(), preferences=prefs)
    assert result.outcome is DispatchOutcome.DEADLETTERED
    dlq: InMemoryDeadLetterStore = deps["dead_letters"]  # type: ignore[assignment]
    assert await dlq.count() == 1


@pytest.mark.asyncio
async def test_dispatch_transient_dead_letters_after_cap() -> None:
    transport = InMemoryEmailTransport(always_fail=True)  # transient, forever
    dispatcher, deps = _dispatcher(
        email_transport=transport,
        retry_policy=RetryPolicy(max_attempts=3, base_s=1.0, jitter=False),
    )
    prefs = NotificationPreferences.defaults("u1")
    notification = _notification()
    outcomes = []
    # First attempt claims the outbox; subsequent retries advance the counter.
    outcomes.append((await dispatcher.dispatch(notification, preferences=prefs)).outcome)
    while outcomes[-1] is DispatchOutcome.RETRY:
        outcomes.append((await dispatcher.retry(notification, preferences=prefs)).outcome)
    assert outcomes[-1] is DispatchOutcome.DEADLETTERED
    dlq: InMemoryDeadLetterStore = deps["dead_letters"]  # type: ignore[assignment]
    assert await dlq.count() == 1


# --------------------------------------------------------------------------- #
# dispatcher: quiet hours + digest gating
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_defers_during_quiet_hours() -> None:
    # Clock at a fixed instant inside an all-day quiet window.
    clock = FakeClock(t=1_700_000_000.0)  # arbitrary
    dispatcher, deps = _dispatcher(clock=clock)
    from datetime import time as _time

    from app.notifications.quiet_hours import QuietHours

    prefs = NotificationPreferences.defaults("u1").model_copy(
        update={"quiet_hours": QuietHours(start=_time(0, 0), end=_time(23, 59), tz_name="UTC")}
    )
    result = await dispatcher.dispatch(_notification(), preferences=prefs)
    assert result.outcome is DispatchOutcome.DEFERRED
    assert result.retry_at is not None
    email: InMemoryEmailTransport = deps["email"]  # type: ignore[assignment]
    assert len(email.sent) == 0


@pytest.mark.asyncio
async def test_urgent_bypasses_quiet_hours() -> None:
    dispatcher, deps = _dispatcher()
    from datetime import time as _time

    from app.notifications.quiet_hours import QuietHours

    prefs = NotificationPreferences.defaults("u1").model_copy(
        update={"quiet_hours": QuietHours(start=_time(0, 0), end=_time(23, 59), tz_name="UTC")}
    )
    urgent = _notification(
        event=DomainEvent.CONFLICT_SURFACED, priority=NotificationPriority.URGENT
    )
    result = await dispatcher.dispatch(urgent, preferences=prefs)
    assert result.outcome is DispatchOutcome.DELIVERED


@pytest.mark.asyncio
async def test_dispatch_digests_when_enabled() -> None:
    digest = InMemoryDigestAccumulator()
    dispatcher, _ = _dispatcher(digest=digest)
    from app.notifications.preferences import DigestCadence

    prefs = NotificationPreferences.defaults("u1").model_copy(
        update={"digest": DigestCadence(enabled=True, interval_minutes=60)}
    )
    result = await dispatcher.dispatch(
        _notification(event=DomainEvent.RENDER_DONE, priority=NotificationPriority.LOW),
        preferences=prefs,
    )
    assert result.outcome is DispatchOutcome.DEFERRED
    assert await digest.pending_user_ids() == ["u1"]


# --------------------------------------------------------------------------- #
# webhook signing + verification
# --------------------------------------------------------------------------- #


def test_webhook_signature_round_trip() -> None:
    signer = WebhookSigner(clock=lambda: 1000.0)
    secret = generate_webhook_secret()
    body = json.dumps({"hello": "world"}).encode()
    header, ts = signer.sign(secret, body)
    assert header.startswith("v1=")
    assert ts == 1000
    assert signer.verify(secret, body, signature=header, timestamp=ts) is True


def test_webhook_signature_rejects_tamper() -> None:
    signer = WebhookSigner(clock=lambda: 1000.0)
    secret = generate_webhook_secret()
    header, ts = signer.sign(secret, b"original")
    assert signer.verify(secret, b"tampered", signature=header, timestamp=ts) is False


def test_webhook_signature_rejects_replay_outside_tolerance() -> None:
    clock = FakeClock(t=1000.0)
    signer = WebhookSigner(clock=clock)
    secret = generate_webhook_secret()
    body = b"x"
    header, ts = signer.sign(secret, body)
    clock.advance(10_000)  # way past tolerance
    assert signer.verify(secret, body, signature=header, timestamp=ts, tolerance_s=300) is False


def test_webhook_signature_wrong_secret_fails() -> None:
    signer = WebhookSigner(clock=lambda: 1000.0)
    body = b"x"
    header, ts = signer.sign("secret-a", body)
    assert signer.verify("secret-b", body, signature=header, timestamp=ts) is False


# --------------------------------------------------------------------------- #
# webhook delivery engine: signing headers, retries, circuit, dead-letter
# --------------------------------------------------------------------------- #


def _endpoint(events: frozenset[str] | None = None) -> WebhookEndpoint:
    return WebhookEndpoint(
        id="ep_1",
        user_id="u1",
        url="https://example.invalid/hook",
        secret=generate_webhook_secret(),
        events=events if events is not None else frozenset({"*"}),
    )


@pytest.mark.asyncio
async def test_webhook_engine_delivers_signed() -> None:
    transport = RecordingWebhookTransport()
    engine = WebhookDeliveryEngine(transport, clock=FakeClock())
    attempt = await engine.deliver(
        _endpoint(), {"k": "v"}, event="book_ready", delivery_id="d1"
    )
    assert attempt.result is WebhookAttemptResult.DELIVERED
    assert len(transport.sent) == 1
    headers = transport.sent[0].headers
    assert headers[SIGNATURE_HEADER].startswith("v1=")
    assert TIMESTAMP_HEADER in headers


@pytest.mark.asyncio
async def test_webhook_engine_verifiable_by_receiver() -> None:
    transport = RecordingWebhookTransport()
    clock = FakeClock()
    endpoint = _endpoint()
    engine = WebhookDeliveryEngine(transport, clock=clock)
    await engine.deliver(endpoint, {"k": "v"}, event="book_ready", delivery_id="d1")
    sent = transport.sent[0]
    # A receiver re-verifies with the shared secret.
    signer = WebhookSigner(clock=clock)
    ts = int(sent.headers[TIMESTAMP_HEADER])
    assert signer.verify(
        endpoint.secret, sent.body, signature=sent.headers[SIGNATURE_HEADER], timestamp=ts
    )


@pytest.mark.asyncio
async def test_webhook_engine_skips_unsubscribed_event() -> None:
    transport = RecordingWebhookTransport()
    engine = WebhookDeliveryEngine(transport, clock=FakeClock())
    endpoint = _endpoint(events=frozenset({"render_done"}))
    attempt = await engine.deliver(
        endpoint, {"k": "v"}, event="book_ready", delivery_id="d1"
    )
    assert attempt.result is WebhookAttemptResult.SKIPPED
    assert transport.sent == []


@pytest.mark.asyncio
async def test_webhook_engine_retries_transient() -> None:
    transport = RecordingWebhookTransport(fail_times=1)
    engine = WebhookDeliveryEngine(
        transport,
        clock=FakeClock(),
        retry_policy=RetryPolicy(max_attempts=3, base_s=2.0, jitter=False),
    )
    state = RetryState()
    endpoint = _endpoint()
    a1 = await engine.deliver(endpoint, {"k": 1}, event="book_ready", delivery_id="d", state=state)
    assert a1.result is WebhookAttemptResult.RETRY
    assert a1.retry_at is not None
    a2 = await engine.deliver(endpoint, {"k": 1}, event="book_ready", delivery_id="d", state=state)
    assert a2.result is WebhookAttemptResult.DELIVERED


@pytest.mark.asyncio
async def test_webhook_engine_dead_letters_after_cap() -> None:
    transport = RecordingWebhookTransport(always_fail=True)
    engine = WebhookDeliveryEngine(
        transport,
        clock=FakeClock(),
        retry_policy=RetryPolicy(max_attempts=2, base_s=1.0, jitter=False),
    )
    state = RetryState()
    endpoint = _endpoint()
    a1 = await engine.deliver(endpoint, {}, event="book_ready", delivery_id="d", state=state)
    assert a1.result is WebhookAttemptResult.RETRY
    a2 = await engine.deliver(endpoint, {}, event="book_ready", delivery_id="d", state=state)
    assert a2.result is WebhookAttemptResult.DEADLETTER


@pytest.mark.asyncio
async def test_webhook_engine_opens_circuit_and_rejects() -> None:
    from app.notifications.circuit import CircuitRegistry

    transport = RecordingWebhookTransport(always_fail=True)
    circuits = CircuitRegistry(failure_threshold=2, reset_timeout_s=30.0)
    engine = WebhookDeliveryEngine(
        transport,
        clock=FakeClock(),
        circuits=circuits,
        retry_policy=RetryPolicy(max_attempts=10, base_s=1.0, jitter=False),
    )
    endpoint = _endpoint()
    # Two failures trip the breaker.
    await engine.deliver(endpoint, {}, event="book_ready", delivery_id="d")
    await engine.deliver(endpoint, {}, event="book_ready", delivery_id="d")
    # Next attempt is rejected by the open circuit without hitting the transport.
    sent_before = len(transport.sent)
    attempt = await engine.deliver(endpoint, {}, event="book_ready", delivery_id="d")
    assert attempt.result is WebhookAttemptResult.CIRCUIT_OPEN
    assert len(transport.sent) == sent_before  # no new send


# --------------------------------------------------------------------------- #
# NotificationService facade (end-to-end, in-memory)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_emit_fans_out_to_channels() -> None:
    email = InMemoryEmailTransport()
    service = NotificationService(email_transport=email)
    recipient = Recipient(user_id="u1", email="r@e.com")
    result = await service.emit(
        DomainEvent.BOOK_READY, recipient=recipient, data={"title": "Dune"}
    )
    # Default prefs route BOOK_READY to in-app + email.
    assert Channel.EMAIL in result.delivered_channels
    assert Channel.IN_APP in result.delivered_channels
    assert len(email.sent) == 1
    inbox = await service.list_inbox("u1")
    assert len(inbox) == 1
    assert "Dune" in inbox[0].subject


@pytest.mark.asyncio
async def test_service_records_delivery_status() -> None:
    service = NotificationService()
    recipient = Recipient(user_id="u1", email="r@e.com")
    await service.emit(DomainEvent.BOOK_READY, recipient=recipient, data={"title": "X"})
    deliveries = await service.list_deliveries("u1")
    assert any(d.status is DeliveryStatus.DELIVERED for d in deliveries)


@pytest.mark.asyncio
async def test_service_webhook_fanout_with_endpoints() -> None:
    transport = RecordingWebhookTransport()
    endpoint = _endpoint(events=frozenset({"book_ready"}))

    async def endpoints_for(user_id: str) -> list[WebhookEndpoint]:
        return [endpoint] if user_id == "u1" else []

    service = NotificationService(
        webhook_transport=transport, endpoints_for=endpoints_for
    )
    recipient = Recipient(user_id="u1", email="r@e.com")
    # Opt the user's webhook channel in for book_ready.
    prefs = NotificationPreferences.defaults("u1").with_event_channels(
        DomainEvent.BOOK_READY, frozenset({Channel.WEBHOOK})
    )
    result = await service.notify(
        DomainEventEnvelope(event=DomainEvent.BOOK_READY, user_id="u1", data={"title": "X"}),
        recipient=recipient,
        preferences=prefs,
    )
    assert result.any_delivered
    assert len(transport.sent) == 1
    assert transport.sent[0].url == endpoint.url


@pytest.mark.asyncio
async def test_service_digest_flush_rolls_up() -> None:
    from app.notifications.preferences import DigestCadence

    clock = FakeClock()
    email = InMemoryEmailTransport()
    recipient = Recipient(user_id="u1", email="r@e.com")
    # Digest on, and RENDER_DONE opted into email so it has a digestable channel.
    prefs = (
        NotificationPreferences.defaults("u1")
        .with_event_channels(
            DomainEvent.RENDER_DONE, frozenset({Channel.IN_APP, Channel.EMAIL})
        )
        .model_copy(update={"digest": DigestCadence(enabled=True, interval_minutes=60)})
    )

    async def prefs_for(_user_id: str) -> NotificationPreferences:
        return prefs

    service = NotificationService(
        email_transport=email, clock=clock, preferences_for=prefs_for
    )
    # Two render-done events accumulate into the digest (email channel, low priority).
    for _ in range(2):
        await service.notify(
            DomainEventEnvelope(event=DomainEvent.RENDER_DONE, user_id="u1", data={}),
            recipient=recipient,
        )
    # In-app delivered immediately; email digested.
    assert len(email.sent) == 0

    async def recipient_for(_user_id: str) -> Recipient:
        return recipient

    # Not yet due.
    assert await service.flush_due_digests(recipient_for=recipient_for) == []
    clock.advance(3600 + 1)
    results = await service.flush_due_digests(recipient_for=recipient_for)
    assert len(results) == 1
    # The digest went out by email.
    assert len(email.sent) == 1
    assert "digest" in email.sent[0].message.subject.lower()


@pytest.mark.asyncio
async def test_service_dead_letter_visible() -> None:
    transport = InMemoryEmailTransport(always_fail=True, permanent=True)
    service = NotificationService(email_transport=transport)
    recipient = Recipient(user_id="u1", email="r@e.com")
    await service.emit(DomainEvent.BOOK_READY, recipient=recipient, data={"title": "X"})
    dls = await service.list_dead_letters("u1")
    assert len(dls) == 1
    assert dls[0].channel is Channel.EMAIL


def test_email_channel_outcome_mapping() -> None:
    # quick sanity that ChannelResult enum has the four states
    assert {r.value for r in ChannelResult} == {
        "delivered",
        "retryable",
        "permanent",
        "unreachable",
    }


# --------------------------------------------------------------------------- #
# bridge: live §5.6 wire event → durable notification
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bridge_emits_book_ready_from_ingest_progress() -> None:
    from app.notifications.bridge import NotificationBridge, book_ready_envelope

    email = InMemoryEmailTransport()
    service = NotificationService(email_transport=email)

    async def recip_book(book_id: str) -> Recipient:
        return Recipient(user_id="u1", email="r@e.com")

    async def recip_session(session_id: str) -> Recipient | None:
        return None

    async def title_for(book_id: str) -> str:
        return "Moby-Dick"

    bridge = NotificationBridge(
        redis=None,
        service=service,
        recipient_for_book=recip_book,
        recipient_for_session=recip_session,
        title_for_book=title_for,
    )
    # The completion event (stage=ready) → BOOK_READY, enriched with the title.
    assert book_ready_envelope({"event": "ingest_progress", "stage": "ready", "book_id": "b1"})
    emitted = await bridge.handle(
        {"event": "ingest_progress", "stage": "ready", "pct": 1.0, "book_id": "b1"}
    )
    assert emitted is True
    assert len(email.sent) == 1
    assert "Moby-Dick" in email.sent[0].message.subject


@pytest.mark.asyncio
async def test_bridge_ignores_non_ready_progress_and_buffer_state() -> None:
    from app.notifications.bridge import NotificationBridge

    service = NotificationService()

    async def recip_book(book_id: str) -> Recipient:
        return Recipient(user_id="u1", email="r@e.com")

    async def recip_session(session_id: str) -> Recipient | None:
        return None

    bridge = NotificationBridge(
        redis=None,
        service=service,
        recipient_for_book=recip_book,
        recipient_for_session=recip_session,
    )
    progress = await bridge.handle({"event": "ingest_progress", "stage": "ocr", "book_id": "b1"})
    assert progress is False
    assert await bridge.handle({"event": "buffer_state", "zone": "committed"}) is False


@pytest.mark.asyncio
async def test_bridge_routes_clip_ready_to_session_owner() -> None:
    from app.notifications.bridge import NotificationBridge

    service = NotificationService()

    async def recip_book(book_id: str) -> Recipient | None:
        return None

    async def recip_session(session_id: str) -> Recipient:
        return Recipient(user_id="u7", email="s@e.com")

    bridge = NotificationBridge(
        redis=None,
        service=service,
        recipient_for_book=recip_book,
        recipient_for_session=recip_session,
    )
    emitted = await bridge.handle(
        {"event": "clip_ready", "shot_id": "shot_9", "session_id": "sess_1", "oss_url": "x"}
    )
    assert emitted is True
    inbox = await service.list_inbox("u7")
    assert len(inbox) == 1
    assert inbox[0].event is DomainEvent.RENDER_DONE


@pytest.mark.asyncio
async def test_bridge_skips_when_no_recipient() -> None:
    from app.notifications.bridge import NotificationBridge

    service = NotificationService()

    async def recip_none(_id: str) -> Recipient | None:
        return None

    bridge = NotificationBridge(
        redis=None,
        service=service,
        recipient_for_book=recip_none,
        recipient_for_session=recip_none,
    )
    emitted = await bridge.handle(
        {"event": "clip_ready", "shot_id": "s", "session_id": "sess_x"}
    )
    assert emitted is False
