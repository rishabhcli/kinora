"""Serde tests — built-in codecs, tombstone round-trip, TypedProducer/Consumer."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.producer import Producer
from app.streaming.log.record import TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.serde import (
    BytesSerde,
    JsonSerde,
    StringSerde,
    TypedConsumer,
    TypedProducer,
)
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    impl: Broker = (
        InMemoryBroker()
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="serde")
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("events", partitions=1))
    return impl


def test_string_serde_roundtrip() -> None:
    assert StringSerde.deserialize(StringSerde.serialize("héllo")) == "héllo"
    assert StringSerde.serialize(None) is None
    assert StringSerde.deserialize(None) is None


def test_json_serde_roundtrip() -> None:
    value = {"page": 12, "chars": ["a", "b"], "n": 3.5}
    assert JsonSerde.deserialize(JsonSerde.serialize(value)) == value
    assert JsonSerde.serialize(None) is None  # tombstone preserved


def test_bytes_serde_is_identity() -> None:
    assert BytesSerde.serialize(b"\x00\x01") == b"\x00\x01"
    assert BytesSerde.deserialize(b"\x00\x01") == b"\x00\x01"


async def test_typed_producer_consumer_json(broker: Broker) -> None:
    producer = TypedProducer(Producer(broker), key_serde=StringSerde, value_serde=JsonSerde)
    await producer.send("events", {"i": 0}, key="book-7", partition=0)
    await producer.send("events", {"i": 1}, key="book-7", partition=0)
    await producer.close()

    consumer = TypedConsumer(
        Consumer(broker), key_serde=StringSerde, value_serde=JsonSerde
    )
    await consumer.raw.assign((TopicPartition("events", 0),))
    batch = await consumer.poll()
    assert [r.value for r in batch] == [{"i": 0}, {"i": 1}]
    assert all(r.key == "book-7" for r in batch)
    assert [r.offset for r in batch] == [0, 1]


async def test_typed_tombstone_decodes_to_none(broker: Broker) -> None:
    producer = TypedProducer(Producer(broker), key_serde=StringSerde, value_serde=JsonSerde)
    await producer.send("events", {"v": 1}, key="k", partition=0)
    await producer.send("events", None, key="k", partition=0)  # tombstone
    await producer.close()

    consumer = TypedConsumer(Consumer(broker), key_serde=StringSerde, value_serde=JsonSerde)
    await consumer.raw.assign((TopicPartition("events", 0),))
    batch = await consumer.poll()
    assert batch[1].value is None
    assert batch[1].key == "k"
