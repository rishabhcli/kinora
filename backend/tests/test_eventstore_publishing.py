"""Message-publisher transport tests (zero infra).

Covers the routing + Redis publishers and their integration with the outbox
relay, using fakes only (a recording Redis double — no real Redis, zero infra).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.eventsourcing.store import (
    NO_STREAM,
    CollectingPublisher,
    EventData,
    InMemoryEventStore,
    OutboxRecord,
    OutboxRelay,
    OutboxStatus,
    RedisMessagePublisher,
    RoutingPublisher,
    channel_for,
)


def _ev(t: str) -> EventData:
    return EventData(event_type=t, payload={"t": t})


def _record(topic: str = "canon", rid: str = "o1") -> OutboxRecord:
    now = datetime.now(UTC)
    return OutboxRecord(
        id=rid,
        event_id="e1",
        global_position=1,
        topic=topic,
        payload={"event_type": "x", "global_position": 1},
        status=OutboxStatus.PENDING,
        attempts=0,
        available_at=now,
        created_at=now,
    )


class FakeRedis:
    """Records publish(channel, message) calls; optionally raises on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self._fail = fail

    async def publish(self, channel: str, message: object) -> int:
        if self._fail:
            raise RuntimeError("redis down")
        self.calls.append((channel, message))
        return 1


# --------------------------------------------------------------------------- #
# channel_for
# --------------------------------------------------------------------------- #


def test_channel_for_namespaces_topic() -> None:
    assert channel_for("canon") == "kinora:es:canon"
    assert channel_for("render").startswith("kinora:es:")


# --------------------------------------------------------------------------- #
# RedisMessagePublisher
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_redis_publisher_publishes_envelope_to_channel() -> None:
    redis = FakeRedis()
    pub = RedisMessagePublisher(redis)  # type: ignore[arg-type]
    await pub.publish(_record(topic="canon"))
    assert len(redis.calls) == 1
    channel, message = redis.calls[0]
    assert channel == "kinora:es:canon"
    assert isinstance(message, dict)
    assert message["topic"] == "canon"
    assert message["event"]["event_type"] == "x"
    assert message["outbox_id"] == "o1"


@pytest.mark.asyncio
async def test_redis_publisher_propagates_transport_error() -> None:
    redis = FakeRedis(fail=True)
    pub = RedisMessagePublisher(redis)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await pub.publish(_record())


# --------------------------------------------------------------------------- #
# RoutingPublisher
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_publisher_dispatches_by_topic() -> None:
    canon = CollectingPublisher()
    render = CollectingPublisher()
    router = RoutingPublisher({"canon": canon, "render": render})

    await router.publish(_record(topic="canon", rid="a"))
    await router.publish(_record(topic="render", rid="b"))
    await router.publish(_record(topic="canon", rid="c"))

    assert {r.id for r in canon.published} == {"a", "c"}
    assert {r.id for r in render.published} == {"b"}


@pytest.mark.asyncio
async def test_routing_publisher_default_fallback() -> None:
    default = CollectingPublisher()
    router = RoutingPublisher({}, default=default)
    await router.publish(_record(topic="unknown"))
    assert len(default.published) == 1


@pytest.mark.asyncio
async def test_routing_publisher_unrouted_raises() -> None:
    router = RoutingPublisher({})
    with pytest.raises(KeyError):
        await router.publish(_record(topic="nope"))


@pytest.mark.asyncio
async def test_routing_publisher_register() -> None:
    sink = CollectingPublisher()
    router = RoutingPublisher({})
    router.register("late", sink)
    await router.publish(_record(topic="late"))
    assert len(sink.published) == 1


# --------------------------------------------------------------------------- #
# Relay + Redis publisher end-to-end (fakes)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_relay_drains_outbox_to_redis() -> None:
    store = InMemoryEventStore()
    await store.append(
        "s", [_ev("a"), _ev("b")], expected_version=NO_STREAM, publish_topic="canon"
    )
    redis = FakeRedis()
    relay = OutboxRelay(store, RedisMessagePublisher(redis), batch_size=10)  # type: ignore[arg-type]

    result = await relay.run_once()
    assert result.published == 2
    assert len(redis.calls) == 2
    channels = {c for c, _ in redis.calls}
    assert channels == {"kinora:es:canon"}
    # Both outbox rows are marked published.
    assert all(r.status is OutboxStatus.PUBLISHED for r in store.all_outbox())


@pytest.mark.asyncio
async def test_relay_redis_failure_backs_off() -> None:
    store = InMemoryEventStore()
    await store.append("s", [_ev("a")], expected_version=NO_STREAM, publish_topic="canon")
    redis = FakeRedis(fail=True)
    relay = OutboxRelay(store, RedisMessagePublisher(redis), batch_size=10, max_attempts=3)  # type: ignore[arg-type]

    result = await relay.run_once()
    assert result.failed == 1
    assert result.published == 0
    row = store.all_outbox()[0]
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 1
    assert row.last_error and "redis down" in row.last_error
