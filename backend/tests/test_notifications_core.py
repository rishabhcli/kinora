"""Unit tests for the notifications platform's pure core (no infra, no network).

Covers backoff, the circuit breaker state machine, quiet-hours math, the template
registry + localization fallback, event mapping, the preference matrix, and the
digest accumulator. These are deterministic with injected clocks/RNG.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, time

import pytest

from app.notifications.backoff import RetryDecision, RetryPolicy, RetryState
from app.notifications.circuit import CircuitBreaker, CircuitRegistry, CircuitState
from app.notifications.digest import DigestBucket, InMemoryDigestAccumulator
from app.notifications.events import DomainEvent, from_session_event
from app.notifications.models import (
    Channel,
    Notification,
    NotificationPriority,
    Recipient,
)
from app.notifications.preferences import NotificationPreferences, overnight_quiet
from app.notifications.quiet_hours import QuietHours
from app.notifications.templates import MessageTemplate, TemplateRegistry

# --------------------------------------------------------------------------- #
# backoff
# --------------------------------------------------------------------------- #


def test_retry_policy_caps_and_grows() -> None:
    policy = RetryPolicy(max_attempts=4, base_s=2.0, factor=2.0, max_delay_s=100.0, jitter=False)
    assert policy.base_delay_for(1) == 2.0
    assert policy.base_delay_for(2) == 4.0
    assert policy.base_delay_for(3) == 8.0
    assert policy.decide(1) is RetryDecision.RETRY
    assert policy.decide(3) is RetryDecision.RETRY
    assert policy.decide(4) is RetryDecision.DEADLETTER


def test_retry_policy_clamps_to_max_delay() -> None:
    policy = RetryPolicy(base_s=2.0, factor=10.0, max_delay_s=50.0, jitter=False)
    assert policy.base_delay_for(10) == 50.0


def test_retry_policy_full_jitter_never_exceeds_ceiling() -> None:
    policy = RetryPolicy(base_s=4.0, factor=2.0, jitter=True)
    rng = random.Random(1234)
    for attempt in range(1, 6):
        ceiling = policy.base_delay_for(attempt)
        for _ in range(50):
            assert 0.0 <= policy.delay_for(attempt, rng=rng) <= ceiling


def test_retry_state_records_history() -> None:
    state = RetryState()
    assert state.record_failure("boom") == 1
    assert state.record_failure("again") == 2
    assert state.last_error == "again"
    assert len(state.errors) == 2


# --------------------------------------------------------------------------- #
# circuit breaker
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_circuit_trips_open_after_threshold() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=10.0, clock=clock)
    assert breaker.state is CircuitState.CLOSED
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


def test_circuit_half_opens_after_timeout_then_closes_on_success() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_s=10.0, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    clock.advance(10.0)
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


def test_circuit_half_open_failure_reopens() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=5.0, clock=clock)
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    clock.advance(5.0)
    assert breaker.allow() is True  # half-open
    breaker.record_failure()  # trial fails
    assert breaker.state is CircuitState.OPEN


def test_circuit_retry_after_decreases_with_time() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=30.0, clock=clock)
    breaker.record_failure()
    assert breaker.retry_after_s() == pytest.approx(30.0)
    clock.advance(20.0)
    assert breaker.retry_after_s() == pytest.approx(10.0)


def test_circuit_registry_isolates_targets() -> None:
    registry = CircuitRegistry(failure_threshold=1, reset_timeout_s=5.0)
    a = registry.get("ep_a")
    b = registry.get("ep_b")
    a.record_failure()
    assert a.state is CircuitState.OPEN
    assert b.state is CircuitState.CLOSED
    assert registry.get("ep_a") is a  # cached


# --------------------------------------------------------------------------- #
# quiet hours
# --------------------------------------------------------------------------- #


def test_quiet_hours_same_day_window() -> None:
    window = QuietHours(start=time(9, 0), end=time(17, 0), tz_name="UTC")
    assert window.is_quiet(datetime(2026, 6, 28, 12, 0, tzinfo=UTC)) is True
    assert window.is_quiet(datetime(2026, 6, 28, 8, 0, tzinfo=UTC)) is False
    assert window.is_quiet(datetime(2026, 6, 28, 17, 0, tzinfo=UTC)) is False  # end exclusive


def test_quiet_hours_overnight_wrap() -> None:
    window = overnight_quiet("UTC")  # 22:00–07:00
    assert window.wraps_midnight is True
    assert window.is_quiet(datetime(2026, 6, 28, 23, 0, tzinfo=UTC)) is True
    assert window.is_quiet(datetime(2026, 6, 28, 3, 0, tzinfo=UTC)) is True
    assert window.is_quiet(datetime(2026, 6, 28, 12, 0, tzinfo=UTC)) is False


def test_quiet_hours_next_open_overnight() -> None:
    window = overnight_quiet("UTC")
    # At 23:00 the next open is 07:00 the *next* day.
    now = datetime(2026, 6, 28, 23, 0, tzinfo=UTC)
    open_at = window.next_open_at(now)
    assert open_at == datetime(2026, 6, 29, 7, 0, tzinfo=UTC)
    # At 03:00 the next open is 07:00 the *same* day.
    now2 = datetime(2026, 6, 28, 3, 0, tzinfo=UTC)
    assert window.next_open_at(now2) == datetime(2026, 6, 28, 7, 0, tzinfo=UTC)


def test_quiet_hours_disabled_or_zero_width_never_quiet() -> None:
    zero_width = QuietHours(start=time(9), end=time(9))
    assert zero_width.is_quiet(datetime(2026, 6, 28, 9, tzinfo=UTC)) is False
    disabled = QuietHours(start=time(0), end=time(23, 59), enabled=False)
    assert disabled.is_quiet(datetime(2026, 6, 28, 12, tzinfo=UTC)) is False


def test_quiet_hours_timezone_aware() -> None:
    # 22:00–07:00 America/New_York. 02:00 UTC = 21:00 (prev day) EST in winter, but
    # 2026-06-28 is summer (EDT, UTC-4): 02:00 UTC = 22:00 EDT → quiet.
    window = QuietHours(start=time(22), end=time(7), tz_name="America/New_York")
    assert window.is_quiet(datetime(2026, 6, 28, 2, 0, tzinfo=UTC)) is True
    # 18:00 UTC = 14:00 EDT → not quiet.
    assert window.is_quiet(datetime(2026, 6, 28, 18, 0, tzinfo=UTC)) is False


# --------------------------------------------------------------------------- #
# templates + localization
# --------------------------------------------------------------------------- #


def test_template_renders_with_interpolation() -> None:
    registry = TemplateRegistry()
    msg = registry.render(
        DomainEvent.BOOK_READY, Channel.EMAIL, locale="en", data={"title": "Moby-Dick"}
    )
    assert "Moby-Dick" in msg.subject
    assert msg.locale == "en"


def test_template_missing_var_left_as_placeholder() -> None:
    registry = TemplateRegistry()
    msg = registry.render(DomainEvent.BUDGET_LOW, Channel.EMAIL, locale="en", data={})
    assert "{remaining_s}" in msg.body  # not crashed


def test_template_locale_fallback_to_default() -> None:
    registry = TemplateRegistry()
    # French isn't in the catalog → falls back to English wording.
    msg = registry.render(
        DomainEvent.BOOK_READY, Channel.EMAIL, locale="fr", data={"title": "X"}
    )
    assert msg.subject  # rendered something


def test_template_localized_spanish() -> None:
    registry = TemplateRegistry()
    msg = registry.render(
        DomainEvent.BOOK_READY, Channel.EMAIL, locale="es", data={"title": "Moby-Dick"}
    )
    assert "listo" in msg.subject.lower()
    assert msg.locale == "es"


def test_template_region_locale_strips_to_base() -> None:
    registry = TemplateRegistry()
    msg = registry.render(
        DomainEvent.BOOK_READY, Channel.EMAIL, locale="es-MX", data={"title": "X"}
    )
    assert "listo" in msg.subject.lower()  # matched the "es" catalog


def test_template_register_override() -> None:
    registry = TemplateRegistry()
    registry.register(
        "en",
        DomainEvent.RENDER_DONE,
        MessageTemplate(subject="Custom", body="b"),
        channel=Channel.PUSH,
    )
    msg = registry.render(DomainEvent.RENDER_DONE, Channel.PUSH, locale="en", data={})
    assert msg.subject == "Custom"


# --------------------------------------------------------------------------- #
# event mapping
# --------------------------------------------------------------------------- #


def test_from_session_event_maps_clip_ready() -> None:
    env = from_session_event(
        {"event": "clip_ready", "shot_id": "shot_1", "oss_url": "x"},
        user_id="u1",
        book_id="b1",
    )
    assert env is not None
    assert env.event is DomainEvent.RENDER_DONE
    assert env.dedup_key == "shot_1"
    assert env.user_id == "u1"


def test_from_session_event_ignores_non_notifiable() -> None:
    assert from_session_event({"event": "buffer_state"}) is None
    assert from_session_event({"event": "keyframe_ready"}) is None


def test_envelope_idempotency_key_stable() -> None:
    from app.notifications.events import DomainEventEnvelope

    env = DomainEventEnvelope(event=DomainEvent.CONFLICT_SURFACED, dedup_key="cf_7")
    assert env.idempotency_key() == "conflict_surfaced:cf_7"


# --------------------------------------------------------------------------- #
# preferences
# --------------------------------------------------------------------------- #


def test_preferences_defaults_reachable() -> None:
    prefs = NotificationPreferences.defaults("u1")
    assert Channel.IN_APP in prefs.channels_for(DomainEvent.BOOK_READY)
    assert Channel.EMAIL in prefs.channels_for(DomainEvent.BOOK_READY)


def test_preferences_opt_out_intersection() -> None:
    prefs = NotificationPreferences.defaults("u1").model_copy(
        update={"enabled_channels": frozenset({Channel.IN_APP})}
    )
    channels = prefs.channels_for(DomainEvent.BOOK_READY)
    assert channels == frozenset({Channel.IN_APP})  # email filtered out globally


def test_preferences_master_mute_suppresses_non_urgent() -> None:
    prefs = NotificationPreferences.defaults("u1").model_copy(update={"enabled": False})
    assert prefs.channels_for(DomainEvent.RENDER_DONE) == frozenset()
    # URGENT still gets through on the always-on rails.
    urgent = prefs.channels_for(
        DomainEvent.CONFLICT_SURFACED, priority=NotificationPriority.URGENT
    )
    assert Channel.IN_APP in urgent


def test_preferences_with_event_channels_copy() -> None:
    prefs = NotificationPreferences.defaults("u1")
    edited = prefs.with_event_channels(DomainEvent.RENDER_DONE, frozenset({Channel.EMAIL}))
    assert edited.matrix[DomainEvent.RENDER_DONE] == frozenset({Channel.EMAIL})
    # original untouched (immutability of the edit)
    assert prefs.matrix[DomainEvent.RENDER_DONE] != frozenset({Channel.EMAIL})


# --------------------------------------------------------------------------- #
# digest accumulator
# --------------------------------------------------------------------------- #


def _notif(user_id: str, event: DomainEvent) -> Notification:
    return Notification(
        id=f"n_{event.value}",
        event=event,
        channel=Channel.EMAIL,
        recipient=Recipient(user_id=user_id, email="a@b.c"),
        idempotency_key=f"{event.value}:x",
    )


@pytest.mark.asyncio
async def test_digest_accumulates_and_flushes_when_due() -> None:
    acc = InMemoryDigestAccumulator()
    await acc.add(_notif("u1", DomainEvent.RENDER_DONE), now=0.0)
    await acc.add(_notif("u1", DomainEvent.RENDER_DONE), now=1.0)
    await acc.add(_notif("u1", DomainEvent.REGEN_DONE), now=2.0)
    # Not due yet.
    assert await acc.due(now=10.0, interval_s=3600.0) == []
    # Due after the interval.
    due = await acc.due(now=4000.0, interval_s=3600.0)
    assert len(due) == 1
    assert due[0].count == 3


@pytest.mark.asyncio
async def test_digest_flush_if_due_respects_interval() -> None:
    acc = InMemoryDigestAccumulator()
    await acc.add(_notif("u1", DomainEvent.RENDER_DONE), now=0.0)
    assert await acc.flush_if_due("u1", now=100.0, interval_s=3600.0) is None
    bucket = await acc.flush_if_due("u1", now=4000.0, interval_s=3600.0)
    assert bucket is not None and bucket.count == 1


def test_digest_bucket_summary_groups_by_event() -> None:
    bucket = DigestBucket(user_id="u1", opened_at=0.0)
    bucket.add(_notif("u1", DomainEvent.RENDER_DONE))
    bucket.add(_notif("u1", DomainEvent.RENDER_DONE))
    summary = bucket.summarize()
    assert "2×" in summary
    assert "render done" in summary
