"""Message publishers + topic routing for the transactional-outbox relay.

The :class:`~contracts.OutboxRelay` is transport-agnostic; this module supplies
the concrete :class:`~contracts.MessagePublisher` implementations it drives:

* :class:`CollectingPublisher` — records every published record (the test fake);
* :class:`RoutingPublisher` — fans a record out to a per-topic publisher (so one
  relay can serve canon / scheduler / render topics with different sinks);
* :class:`RedisMessagePublisher` — publishes to a Redis pub/sub channel via the
  project's :class:`~app.redis.client.RedisClient` (the production transport).

The Redis client is **injected**, so every publisher here runs against a fake in
tests (zero infra, zero credits). The channel-naming policy lives in
:func:`channel_for` so it is one obvious place to change.

This pairs with the consumer side: a subscriber decodes the JSON envelope the
relay publishes (the outbox row's ``payload``) and uses the
:class:`~contracts.InboxRepository` to dedupe redeliveries — effectively-once
processing over Redis's at-most-once pub/sub becomes at-least-once + idempotent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.eventsourcing.store.contracts import MessagePublisher, OutboxRecord

if TYPE_CHECKING:
    from app.redis.client import RedisClient

#: Prefix for every event-store pub/sub channel (namespaced under the app's
#: ``kinora:`` Redis keyspace convention).
CHANNEL_PREFIX = "kinora:es:"


def channel_for(topic: str) -> str:
    """The Redis channel name for an outbox ``topic`` (the routing policy)."""
    return f"{CHANNEL_PREFIX}{topic}"


class CollectingPublisher:
    """A :class:`~contracts.MessagePublisher` that records everything (tests)."""

    def __init__(self) -> None:
        self.published: list[OutboxRecord] = []

    async def publish(self, record: OutboxRecord) -> None:
        self.published.append(record)

    def topics(self) -> set[str]:
        return {r.topic for r in self.published}


class RedisMessagePublisher:
    """Publishes outbox records to a Redis pub/sub channel per topic.

    The published message is the outbox row's ``payload`` (the projected event:
    stream/version/global_position/type/payload/metadata), so a subscriber gets a
    self-describing envelope. ``RedisClient.publish`` raises on a transport error,
    which the relay treats as a transient failure (backoff → DLQ).
    """

    def __init__(
        self,
        redis: RedisClient,
        *,
        channel_namer: Callable[[str], str] = channel_for,
    ) -> None:
        self._redis = redis
        self._channel_namer = channel_namer

    async def publish(self, record: OutboxRecord) -> None:
        channel = self._channel_namer(record.topic)
        await self._redis.publish(
            channel,
            {
                "outbox_id": record.id,
                "global_position": record.global_position,
                "topic": record.topic,
                "event": record.payload,
            },
        )


class RoutingPublisher:
    """Dispatches each record to a per-topic publisher, with an optional default.

    Lets one relay serve heterogeneous sinks: e.g. canon → projection rebuild,
    render → the websocket fan-out. An unrouted topic with no ``default`` raises
    (the relay treats it as transient and retries, surfacing a misconfiguration
    instead of silently dropping).
    """

    def __init__(
        self,
        routes: dict[str, MessagePublisher],
        *,
        default: MessagePublisher | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._default = default

    def register(self, topic: str, publisher: MessagePublisher) -> None:
        self._routes[topic] = publisher

    async def publish(self, record: OutboxRecord) -> None:
        publisher = self._routes.get(record.topic, self._default)
        if publisher is None:
            raise KeyError(f"no publisher registered for topic {record.topic!r}")
        await publisher.publish(record)


#: A handler that consumes a published event envelope (the subscriber side).
PublishedHandler = Callable[[dict], Awaitable[None]]


__all__ = [
    "CHANNEL_PREFIX",
    "CollectingPublisher",
    "PublishedHandler",
    "RedisMessagePublisher",
    "RoutingPublisher",
    "channel_for",
]
