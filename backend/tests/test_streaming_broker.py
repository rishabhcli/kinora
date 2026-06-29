"""Broker-contract tests — run identically against the in-memory and Redis brokers.

Parametrising over both implementations is the payoff of the ``Broker`` protocol:
every behaviour below is verified to hold the same way on the zero-infra
in-memory broker and the Redis-Streams broker (over its in-process Redis double),
so the two stay interchangeable for the sibling facets.
"""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker, ProduceContext
from app.streaming.log.errors import (
    FencedProducerError,
    OffsetOutOfRangeError,
    PartitionNotFoundError,
    SequenceError,
    TopicExistsError,
    TopicNotFoundError,
)
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.record import TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    if request.param == "memory":
        impl: Broker = InMemoryBroker()
    else:
        impl = RedisStreamsBroker(FakeStreamRedis(), namespace="test")
    await impl.start()
    return impl


async def _seed(broker: Broker, name: str = "t", partitions: int = 3) -> None:
    await broker.create_topic(TopicConfig.deleted(name, partitions=partitions))


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


async def test_create_describe_list_delete_topic(broker: Broker) -> None:
    await _seed(broker, "beats", partitions=4)
    assert "beats" in await broker.topics()
    assert (await broker.describe_topic("beats")).partitions == 4
    assert await broker.partitions_for("beats") == 4
    await broker.delete_topic("beats")
    assert "beats" not in await broker.topics()


async def test_duplicate_topic_rejected(broker: Broker) -> None:
    await _seed(broker, "beats")
    with pytest.raises(TopicExistsError):
        await _seed(broker, "beats")


async def test_unknown_topic_raises(broker: Broker) -> None:
    with pytest.raises(TopicNotFoundError):
        await broker.describe_topic("ghost")


async def test_bad_partition_raises(broker: Broker) -> None:
    await _seed(broker, "t", partitions=2)
    with pytest.raises(PartitionNotFoundError):
        await broker.produce("t", 9, key=None, value=b"x")


# --------------------------------------------------------------------------- #
# Produce / fetch
# --------------------------------------------------------------------------- #


async def test_produce_assigns_monotonic_offsets(broker: Broker) -> None:
    await _seed(broker)
    offsets = [(await broker.produce("t", 0, key=None, value=bytes([i]))).offset for i in range(5)]
    assert offsets == [0, 1, 2, 3, 4]


async def test_fetch_reads_back_in_order(broker: Broker) -> None:
    await _seed(broker)
    for i in range(6):
        await broker.produce("t", 1, key=b"k", value=bytes([i]), timestamp_ms=i)
    result = await broker.fetch("t", 1, 0, max_records=10)
    assert [r.value for r in result.records] == [bytes([i]) for i in range(6)]
    assert result.high_watermark == 6


async def test_fetch_window_resume(broker: Broker) -> None:
    await _seed(broker)
    for i in range(10):
        await broker.produce("t", 0, key=None, value=bytes([i]))
    first = await broker.fetch("t", 0, 0, max_records=4)
    assert [r.offset for r in first.records] == [0, 1, 2, 3]
    second = await broker.fetch("t", 0, first.next_offset, max_records=4)
    assert [r.offset for r in second.records] == [4, 5, 6, 7]


async def test_fetch_out_of_range(broker: Broker) -> None:
    await _seed(broker)
    await broker.produce("t", 0, key=None, value=b"x")
    with pytest.raises(OffsetOutOfRangeError):
        await broker.fetch("t", 0, 99)


async def test_headers_and_tombstones_roundtrip(broker: Broker) -> None:
    await _seed(broker)
    await broker.produce("t", 0, key=b"k", value=b"v", headers=(("trace", b"abc"),))
    await broker.produce("t", 0, key=b"k", value=None)  # tombstone
    recs = (await broker.fetch("t", 0, 0)).records
    assert recs[0].headers == (("trace", b"abc"),)
    assert recs[1].is_tombstone


async def test_beginning_and_end_offsets(broker: Broker) -> None:
    await _seed(broker)
    tp = TopicPartition("t", 2)
    for _ in range(3):
        await broker.produce("t", 2, key=None, value=b"x")
    assert (await broker.beginning_offsets((tp,)))[tp] == 0
    assert (await broker.end_offsets((tp,)))[tp] == 3


async def test_offsets_for_times(broker: Broker) -> None:
    await _seed(broker)
    tp = TopicPartition("t", 0)
    for i in range(4):
        await broker.produce("t", 0, key=None, value=b"x", timestamp_ms=i * 100)
    assert (await broker.offsets_for_times({tp: 150}))[tp] == 2
    assert (await broker.offsets_for_times({tp: 9999}))[tp] is None


# --------------------------------------------------------------------------- #
# Idempotence + fencing
# --------------------------------------------------------------------------- #


async def test_idempotent_duplicate_is_deduplicated(broker: Broker) -> None:
    await _seed(broker)
    ctx0 = ProduceContext(producer_id="p1", sequence=0)
    m1 = await broker.produce("t", 0, key=None, value=b"a", ctx=ctx0)
    # Retried send with the same sequence is a benign duplicate (same offset, no new append).
    m_dup = await broker.produce("t", 0, key=None, value=b"a", ctx=ctx0)
    assert m1.offset == m_dup.offset
    assert (await broker.end_offsets((TopicPartition("t", 0),)))[TopicPartition("t", 0)] == 1


async def test_idempotent_gap_raises_sequence_error(broker: Broker) -> None:
    await _seed(broker)
    ctx0 = ProduceContext(producer_id="p1", sequence=0)
    await broker.produce("t", 0, key=None, value=b"a", ctx=ctx0)
    with pytest.raises(SequenceError):
        await broker.produce(
            "t", 0, key=None, value=b"c", ctx=ProduceContext(producer_id="p1", sequence=5)
        )


async def test_fenced_producer_on_lower_epoch(broker: Broker) -> None:
    await _seed(broker)
    await broker.produce(
        "t", 0, key=None, value=b"a", ctx=ProduceContext(producer_id="p1", epoch=2)
    )
    with pytest.raises(FencedProducerError):
        await broker.produce(
            "t", 0, key=None, value=b"b", ctx=ProduceContext(producer_id="p1", epoch=1)
        )


# --------------------------------------------------------------------------- #
# Offset store
# --------------------------------------------------------------------------- #


async def test_commit_and_fetch_committed(broker: Broker) -> None:
    await _seed(broker)
    tp = TopicPartition("t", 0)
    assert (await broker.fetch_committed("g", (tp,)))[tp] is None
    await broker.commit_offsets("g", {tp: 7})
    assert (await broker.fetch_committed("g", (tp,)))[tp] == 7
    assert await broker.list_committed("g") == {tp: 7}


# --------------------------------------------------------------------------- #
# Maintenance (retention + compaction) over the broker
# --------------------------------------------------------------------------- #


async def test_broker_maintain_compacts(broker: Broker) -> None:
    await broker.create_topic(TopicConfig.compacted("c", partitions=1))
    for v in (b"1", b"2", b"3"):
        await broker.produce("c", 0, key=b"a", value=v, timestamp_ms=1)
    removed = await broker.maintain(now=10_000)
    assert removed == 2
    tp = TopicPartition("c", 0)
    start = (await broker.beginning_offsets((tp,)))[tp]
    recs = (await broker.fetch("c", 0, start)).records
    assert [r.value for r in recs] == [b"3"]
