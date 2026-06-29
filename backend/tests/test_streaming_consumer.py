"""Consumer tests — assignment, poll, offset reset, commit, seek, lag, streaming."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer, ConsumerConfig
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
        else RedisStreamsBroker(FakeStreamRedis(), namespace="cons")
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("beats", partitions=2))
    return impl


async def _produce(broker: Broker, n: int, *, partition: int = 0) -> None:
    producer = Producer(broker)
    for i in range(n):
        await producer.send_and_wait(
            ProducerRecord.from_str("beats", f"v{i}", key="k", partition=partition)
        )
    await producer.close()


async def test_manual_assign_and_poll(broker: Broker) -> None:
    await _produce(broker, 5)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    batch = await consumer.poll()
    assert [r.value_str() for r in batch] == [f"v{i}" for i in range(5)]
    # Position advanced; a second poll is empty (caught up).
    assert await consumer.poll() == []


async def test_subscribe_joins_group_and_assigns_partitions(broker: Broker) -> None:
    consumer = Consumer(broker, config=ConsumerConfig(group_id="renderers"))
    await consumer.subscribe(("beats",))
    assert set(consumer.assignment) == {TopicPartition("beats", 0), TopicPartition("beats", 1)}
    assert consumer.member_id is not None


async def test_earliest_vs_latest_reset(broker: Broker) -> None:
    await _produce(broker, 3)
    early = Consumer(broker, config=ConsumerConfig(auto_offset_reset="earliest"))
    await early.assign((TopicPartition("beats", 0),))
    assert len(await early.poll()) == 3

    late = Consumer(broker, config=ConsumerConfig(auto_offset_reset="latest"))
    await late.assign((TopicPartition("beats", 0),))
    assert await late.poll() == []  # starts at the end


async def test_commit_then_resume_from_committed(broker: Broker) -> None:
    await _produce(broker, 6)
    c1 = Consumer(broker, config=ConsumerConfig(group_id="g"))
    await c1.assign((TopicPartition("beats", 0),))
    batch = await c1.poll(max_records=3)
    assert len(batch) == 3
    await c1.commit()  # commits next-offset = 3

    # A fresh consumer in the same group resumes after the committed offset.
    c2 = Consumer(broker, config=ConsumerConfig(group_id="g"))
    await c2.assign((TopicPartition("beats", 0),))
    resumed = await c2.poll()
    assert [r.value_str() for r in resumed] == ["v3", "v4", "v5"]


async def test_seek_replays(broker: Broker) -> None:
    await _produce(broker, 4)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll()  # drain to end
    consumer.seek(TopicPartition("beats", 0), 1)
    replay = await consumer.poll()
    assert [r.offset for r in replay] == [1, 2, 3]


async def test_seek_to_beginning_and_end(broker: Broker) -> None:
    await _produce(broker, 4)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.seek_to_end()
    assert await consumer.poll() == []
    await consumer.seek_to_beginning()
    assert len(await consumer.poll()) == 4


async def test_lag_reporting(broker: Broker) -> None:
    await _produce(broker, 10)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll(max_records=4)
    lag = await consumer.lag()
    assert lag[TopicPartition("beats", 0)] == 6


async def test_async_iteration_streams_records(broker: Broker) -> None:
    await _produce(broker, 3)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    seen = []
    async for record in consumer:
        seen.append(record.value_str())
        if len(seen) == 3:
            break
    assert seen == ["v0", "v1", "v2"]


async def test_auto_commit_persists_on_poll(broker: Broker) -> None:
    await _produce(broker, 3)
    consumer = Consumer(
        broker,
        config=ConsumerConfig(group_id="g", enable_auto_commit=True, auto_commit_interval_ms=0),
    )
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll()
    committed = await consumer.committed()
    assert committed[TopicPartition("beats", 0)] == 3


async def test_pause_skips_partition_then_resume_reads_it(broker: Broker) -> None:
    await _produce(broker, 3, partition=0)
    await _produce(broker, 2, partition=1)
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0), TopicPartition("beats", 1)))

    consumer.pause(TopicPartition("beats", 0))
    assert consumer.paused() == (TopicPartition("beats", 0),)
    batch = await consumer.poll()
    # Only partition 1 is read while partition 0 is paused.
    assert {r.partition for r in batch} == {1}

    consumer.resume(TopicPartition("beats", 0))
    assert consumer.paused() == ()
    batch2 = await consumer.poll()
    assert {r.partition for r in batch2} == {0}


async def test_pause_unassigned_partition_raises(broker: Broker) -> None:
    consumer = Consumer(broker)
    await consumer.assign((TopicPartition("beats", 0),))
    with pytest.raises(KeyError):
        consumer.pause(TopicPartition("beats", 1))


async def test_subscribe_to_multiple_topics(broker: Broker) -> None:
    # A second topic for the multi-topic subscription.
    await broker.create_topic(TopicConfig.deleted("logs", partitions=1))
    producer = Producer(broker)
    await producer.send_and_wait(ProducerRecord.from_str("beats", "b", key="k", partition=0))
    await producer.send_and_wait(ProducerRecord.from_str("logs", "l", key="k", partition=0))
    await producer.close()

    consumer = Consumer(broker, config=ConsumerConfig(group_id="multi"))
    await consumer.subscribe(("beats", "logs"))
    topics_assigned = {tp.topic for tp in consumer.assignment}
    assert topics_assigned == {"beats", "logs"}
    polled = await consumer.poll()
    values = sorted(r.value_str() or "" for r in polled)
    assert values == ["b", "l"]
