"""Outbox relay (publish + backoff + DLQ) and idempotent inbox — zero infra.

Drives the :class:`OutboxRelay` over the in-memory store (which is also a fully
working :class:`OutboxRepository`), plus a recording / scripted-failure publisher,
to verify the §12.1 reliable-publish guarantees: at-least-once delivery, ordered
draining, exponential backoff on transient failure, and dead-lettering after the
attempt cap. Inbox idempotency is verified against the in-memory store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.eventsourcing.store import (
    NO_STREAM,
    EventData,
    InMemoryEventStore,
    OutboxRecord,
    OutboxRelay,
    OutboxStatus,
)
from app.eventsourcing.store.outbox import backoff_delay


def _ev(t: str) -> EventData:
    return EventData(event_type=t, payload={"t": t})


class RecordingPublisher:
    """Records every published record; never fails."""

    def __init__(self) -> None:
        self.published: list[OutboxRecord] = []

    async def publish(self, record: OutboxRecord) -> None:
        self.published.append(record)


class FlakyPublisher:
    """Fails the first ``fail_times`` calls for a given event id, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self._seen: dict[str, int] = {}
        self.published: list[OutboxRecord] = []

    async def publish(self, record: OutboxRecord) -> None:
        n = self._seen.get(record.event_id, 0)
        self._seen[record.event_id] = n + 1
        if n < self._fail_times:
            raise RuntimeError("transient publish error")
        self.published.append(record)


class AlwaysFailPublisher:
    async def publish(self, record: OutboxRecord) -> None:
        raise RuntimeError("permanent failure")


# --------------------------------------------------------------------------- #
# Backoff schedule
# --------------------------------------------------------------------------- #


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delay(0, base_seconds=2, cap_seconds=300) == timedelta(seconds=2)
    assert backoff_delay(1, base_seconds=2, cap_seconds=300) == timedelta(seconds=4)
    assert backoff_delay(3, base_seconds=2, cap_seconds=300) == timedelta(seconds=16)
    # Cap applies for large attempt counts.
    assert backoff_delay(20, base_seconds=2, cap_seconds=300) == timedelta(seconds=300)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_relay_publishes_pending_rows_in_order() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s", [_ev("a"), _ev("b"), _ev("c")], expected_version=NO_STREAM, publish_topic="canon"
    )
    pub = RecordingPublisher()
    relay = OutboxRelay(store, pub, batch_size=10)

    result = await relay.run_once()
    assert result.claimed == 3
    assert result.published == 3
    assert [r.payload["event_type"] for r in pub.published] == ["a", "b", "c"]
    # All marked published; a second pass claims nothing.
    rows = store.all_outbox()
    assert all(r.status is OutboxStatus.PUBLISHED for r in rows)
    again = await relay.run_once()
    assert again.claimed == 0


@pytest.mark.asyncio
async def test_relay_drain_flushes_everything() -> None:
    store = InMemoryEventStore()
    for i in range(25):
        await store.append(f"s{i}", [_ev(f"e{i}")], expected_version=NO_STREAM, publish_topic="t")
    pub = RecordingPublisher()
    relay = OutboxRelay(store, pub, batch_size=10)
    total = await relay.drain()
    assert total.published == 25
    assert len(pub.published) == 25


# --------------------------------------------------------------------------- #
# Backoff + retry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_publish_backs_off_then_succeeds() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a")], expected_version=NO_STREAM, publish_topic="canon")
    pub = FlakyPublisher(fail_times=1)
    relay = OutboxRelay(store, pub, batch_size=10, base_backoff_seconds=2, max_attempts=5)

    t0 = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    first = await relay.run_once(now=t0)
    assert first.failed == 1 and first.published == 0
    row = store.all_outbox()[0]
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 1
    assert row.available_at == t0 + timedelta(seconds=2)  # backoff applied
    assert row.last_error and "transient" in row.last_error

    # Before backoff elapses, nothing is claimed.
    too_soon = await relay.run_once(now=t0 + timedelta(seconds=1))
    assert too_soon.claimed == 0

    # After backoff, the retry succeeds.
    later = await relay.run_once(now=t0 + timedelta(seconds=3))
    assert later.published == 1
    assert store.all_outbox()[0].status is OutboxStatus.PUBLISHED


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a")], expected_version=NO_STREAM, publish_topic="canon")
    pub = AlwaysFailPublisher()
    relay = OutboxRelay(store, pub, batch_size=10, base_backoff_seconds=1, max_attempts=3)

    now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    dead = False
    for _ in range(10):
        result = await relay.run_once(now=now)
        if result.dead_lettered:
            dead = True
            break
        # Advance time past the backoff so the row is reclaimable.
        now = now + timedelta(seconds=3600)
    assert dead
    row = store.all_outbox()[0]
    assert row.status is OutboxStatus.DEAD
    assert row.attempts == 3
    # A dead row is never claimed again.
    assert (await relay.run_once(now=now + timedelta(days=1))).claimed == 0


@pytest.mark.asyncio
async def test_partial_batch_failure_publishes_the_rest() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a"), _ev("b")], expected_version=NO_STREAM, publish_topic="canon")
    rows = store.all_outbox()
    target_event = rows[0].event_id

    class FailOne:
        published: list[OutboxRecord] = []

        async def publish(self, record: OutboxRecord) -> None:
            if record.event_id == target_event:
                raise RuntimeError("boom")
            self.published.append(record)

    pub = FailOne()
    relay = OutboxRelay(store, pub, batch_size=10, max_attempts=5)
    result = await relay.run_once()
    assert result.published == 1
    assert result.failed == 1
    statuses = {r.event_id: r.status for r in store.all_outbox()}
    assert statuses[target_event] is OutboxStatus.PENDING
    other = next(eid for eid in statuses if eid != target_event)
    assert statuses[other] is OutboxStatus.PUBLISHED


# --------------------------------------------------------------------------- #
# Idempotent inbox
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inbox_marks_once_and_detects_replay() -> None:
    store = InMemoryEventStore()
    assert not await store.already_processed("proj-shots", "evt-1")
    assert await store.mark_processed("proj-shots", "evt-1", result={"ok": True})
    assert await store.already_processed("proj-shots", "evt-1")
    # A redelivery: mark returns False (already recorded).
    assert not await store.mark_processed("proj-shots", "evt-1")
    # A different consumer tracks independently.
    assert not await store.already_processed("proj-search", "evt-1")
    assert await store.mark_processed("proj-search", "evt-1")
