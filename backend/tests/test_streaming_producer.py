"""Producer tests — partition resolution, batching, idempotent retry dedup."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker, ProduceContext
from app.streaming.log.errors import IllegalStateError
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.producer import Producer, ProducerConfig
from app.streaming.log.record import ProducerRecord, RecordMetadata, TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    impl: Broker = (
        InMemoryBroker()
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="prod")
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("beats", partitions=4))
    return impl


async def test_keyed_records_land_in_one_partition(broker: Broker) -> None:
    producer = Producer(broker)
    metas = [
        await producer.send_and_wait(ProducerRecord.from_str("beats", f"v{i}", key="book-7"))
        for i in range(10)
    ]
    await producer.close()
    partitions = {m.partition for m in metas}
    assert len(partitions) == 1  # per-key ordering: all in one partition
    # And in append order.
    assert [m.offset for m in metas] == list(range(10))


async def test_explicit_partition_bypasses_partitioner(broker: Broker) -> None:
    producer = Producer(broker)
    meta = await producer.send_and_wait(
        ProducerRecord.from_str("beats", "v", key="anything", partition=2)
    )
    assert meta.partition == 2


async def test_send_returns_future_resolved_on_flush(broker: Broker) -> None:
    producer = Producer(broker, config=ProducerConfig(linger_ms=10, batch_size=100))
    fut = await producer.send(ProducerRecord.from_str("beats", "v", key="k"))
    assert not fut.done()  # lingering; not yet flushed
    await producer.flush()
    meta: RecordMetadata = await fut
    assert meta.offset == 0


async def test_idempotent_producer_dedupes_on_replayed_sequence(broker: Broker) -> None:
    producer = Producer(broker, config=ProducerConfig(enable_idempotence=True))
    await producer.send_and_wait(ProducerRecord.from_str("beats", "v0", key="k", partition=0))
    pid = producer.producer_id
    assert pid is not None

    # Simulate a duplicated network delivery: re-send sequence 0 directly.
    dup = await broker.produce(
        "beats", 0, key=b"k", value=b"v0", ctx=ProduceContext(producer_id=pid, sequence=0)
    )
    end = (await broker.end_offsets((TopicPartition("beats", 0),)))[TopicPartition("beats", 0)]
    assert end == 1  # the duplicate did not append a second record
    assert dup.offset == 0


async def test_non_idempotent_producer_uses_plain_context(broker: Broker) -> None:
    producer = Producer(broker, config=ProducerConfig(enable_idempotence=False))
    assert producer.producer_id is None
    meta = await producer.send_and_wait(ProducerRecord.from_str("beats", "v", key="k"))
    assert meta.offset == 0


async def test_send_after_close_raises(broker: Broker) -> None:
    producer = Producer(broker)
    await producer.close()
    with pytest.raises(IllegalStateError):
        await producer.send(ProducerRecord.from_str("beats", "v", key="k"))


async def test_transaction_methods_require_transactional_id(broker: Broker) -> None:
    producer = Producer(broker)  # no transactional_id
    with pytest.raises(IllegalStateError):
        await producer.begin_transaction()
