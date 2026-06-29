"""Transaction / exactly-once tests — atomic commit, abort, and read-process-write EOS."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.errors import IllegalStateError
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.producer import Producer, ProducerConfig
from app.streaming.log.record import ProducerRecord, TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    impl: Broker = (
        InMemoryBroker()
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="txn")
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("in", partitions=1))
    await impl.create_topic(TopicConfig.deleted("out", partitions=1))
    return impl


def _txn_producer(broker: Broker, tid: str = "tx-1") -> Producer:
    return Producer(broker, config=ProducerConfig(transactional_id=tid))


async def test_committed_transaction_appends_atomically(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    await producer.begin_transaction()
    await producer.send(ProducerRecord.from_str("out", "a", key="k", partition=0))
    await producer.send(ProducerRecord.from_str("out", "b", key="k", partition=0))

    tp = TopicPartition("out", 0)
    # Before commit, nothing is visible in the log.
    assert (await broker.end_offsets((tp,)))[tp] == 0

    metas = await producer.commit_transaction()
    assert [m.offset for m in metas] == [0, 1]
    assert (await broker.end_offsets((tp,)))[tp] == 2


async def test_aborted_transaction_discards_records(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    await producer.begin_transaction()
    await producer.send(ProducerRecord.from_str("out", "a", key="k", partition=0))
    await producer.abort_transaction()

    tp = TopicPartition("out", 0)
    assert (await broker.end_offsets((tp,)))[tp] == 0  # nothing committed


async def test_transaction_context_manager_commits_on_success(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    async with producer.transaction():
        await producer.send(ProducerRecord.from_str("out", "x", key="k", partition=0))
    tp = TopicPartition("out", 0)
    assert (await broker.end_offsets((tp,)))[tp] == 1


async def test_transaction_context_manager_aborts_on_error(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    with pytest.raises(RuntimeError):
        async with producer.transaction():
            await producer.send(ProducerRecord.from_str("out", "x", key="k", partition=0))
            raise RuntimeError("boom")
    tp = TopicPartition("out", 0)
    assert (await broker.end_offsets((tp,)))[tp] == 0  # rolled back


async def test_double_begin_is_illegal(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    await producer.begin_transaction()
    with pytest.raises(IllegalStateError):
        await producer.begin_transaction()


async def test_commit_without_begin_is_illegal(broker: Broker) -> None:
    producer = _txn_producer(broker)
    await producer.init_transactions()
    with pytest.raises(IllegalStateError):
        await producer.commit_transaction()


async def test_exactly_once_read_process_write(broker: Broker) -> None:
    # Seed the input topic.
    seed = Producer(broker)
    for i in range(3):
        await seed.send_and_wait(ProducerRecord.from_str("in", f"v{i}", key="k", partition=0))
    await seed.close()

    consumer = Consumer(broker, config=ConsumerConfig(group_id="processor"))
    await consumer.assign((TopicPartition("in", 0),))
    batch = await consumer.poll()
    assert len(batch) == 3

    # Atomically: write the transformed output AND commit the input offsets.
    producer = _txn_producer(broker, "etl-1")
    await producer.init_transactions()
    await producer.begin_transaction()
    for record in batch:
        await producer.send(
            ProducerRecord.from_str("out", f"{record.value_str()}!", key="k", partition=0)
        )
    in_tp = TopicPartition("in", 0)
    await producer.send_offsets_to_transaction({in_tp: 3}, "processor")
    await producer.commit_transaction()

    # Output written exactly once...
    out_tp = TopicPartition("out", 0)
    assert (await broker.end_offsets((out_tp,)))[out_tp] == 3
    out_recs = (await broker.fetch("out", 0, 0)).records
    assert [r.value_str() for r in out_recs] == ["v0!", "v1!", "v2!"]
    # ...and the consumer's progress was committed in the same transaction.
    assert (await broker.fetch_committed("processor", (in_tp,)))[in_tp] == 3


async def test_aborted_eos_leaves_offsets_uncommitted(broker: Broker) -> None:
    seed = Producer(broker)
    await seed.send_and_wait(ProducerRecord.from_str("in", "v0", key="k", partition=0))
    await seed.close()

    producer = _txn_producer(broker, "etl-2")
    await producer.init_transactions()
    await producer.begin_transaction()
    await producer.send(ProducerRecord.from_str("out", "x", key="k", partition=0))
    await producer.send_offsets_to_transaction({TopicPartition("in", 0): 1}, "processor")
    await producer.abort_transaction()

    in_tp = TopicPartition("in", 0)
    out_tp = TopicPartition("out", 0)
    assert (await broker.end_offsets((out_tp,)))[out_tp] == 0
    assert (await broker.fetch_committed("processor", (in_tp,)))[in_tp] is None
