"""Admin client tests — idempotent topic creation, describe, group lag."""

from __future__ import annotations

import pytest

from app.streaming.log.admin import Admin
from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.errors import TopicNotFoundError
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.producer import Producer
from app.streaming.log.record import ProducerRecord, TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    impl: Broker = (
        InMemoryBroker()
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="admin")
    )
    await impl.start()
    return impl


async def test_create_topic_if_absent(broker: Broker) -> None:
    admin = Admin(broker)
    assert await admin.create_topic_if_absent(TopicConfig.deleted("t", partitions=2)) is True
    assert await admin.create_topic_if_absent(TopicConfig.deleted("t", partitions=2)) is False
    assert "t" in await broker.topics()


async def test_ensure_topics_creates_missing_only(broker: Broker) -> None:
    admin = Admin(broker)
    await admin.create_topic_if_absent(TopicConfig.deleted("a"))
    created = await admin.ensure_topics(
        TopicConfig.deleted("a"), TopicConfig.deleted("b"), TopicConfig.deleted("c")
    )
    assert created == ["b", "c"]


async def test_describe_reports_offset_windows(broker: Broker) -> None:
    await broker.create_topic(TopicConfig.deleted("beats", partitions=2))
    producer = Producer(broker)
    for i in range(5):
        await producer.send_and_wait(ProducerRecord("beats", value=bytes([i]), partition=0))
    for i in range(2):
        await producer.send_and_wait(ProducerRecord("beats", value=bytes([i]), partition=1))
    await producer.close()

    admin = Admin(broker)
    desc = await admin.describe("beats")
    assert desc.config.partitions == 2
    assert desc.total_records == 7
    by_partition = {p.partition: p.record_count for p in desc.partitions}
    assert by_partition == {0: 5, 1: 2}
    assert await admin.topic_size("beats") == 7


async def test_group_lag(broker: Broker) -> None:
    await broker.create_topic(TopicConfig.deleted("beats", partitions=1))
    producer = Producer(broker)
    for i in range(10):
        await producer.send_and_wait(ProducerRecord("beats", value=bytes([i]), partition=0))
    await producer.close()

    admin = Admin(broker)
    # No commits yet → full lag.
    lag = await admin.group_lag("g", "beats")
    assert lag.total == 10

    # Consume + commit 4 → lag drops to 6.
    consumer = Consumer(broker, config=ConsumerConfig(group_id="g"))
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll(max_records=4)
    await consumer.commit()
    lag2 = await admin.group_lag("g", "beats")
    assert lag2.total == 6
    assert lag2.per_partition[TopicPartition("beats", 0)] == 6


async def test_group_lag_unknown_topic(broker: Broker) -> None:
    admin = Admin(broker)
    with pytest.raises(TopicNotFoundError):
        await admin.group_lag("g", "ghost")
