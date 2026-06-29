"""LogCleaner tests — single sweep, stats, error survival, background loop."""

from __future__ import annotations

import asyncio

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.cleaner import LogCleaner
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
        else RedisStreamsBroker(FakeStreamRedis(), namespace="clean")
    )
    await impl.start()
    return impl


async def test_sweep_compacts_and_records_stats(broker: Broker) -> None:
    await broker.create_topic(TopicConfig.compacted("c", partitions=1))
    producer = Producer(broker)
    for v in (b"1", b"2", b"3"):
        await producer.send_and_wait(ProducerRecord("c", value=v, key=b"a", partition=0))
    await producer.close()

    cleaner = LogCleaner(broker, clock=lambda: 10_000)
    removed = await cleaner.sweep_once()
    assert removed == 2
    assert cleaner.stats.sweeps == 1
    assert cleaner.stats.records_removed == 2
    assert cleaner.stats.last_error is None


async def test_sweep_survives_broker_error() -> None:
    class _Boom:
        async def maintain(self, *, now: int | None = None) -> int:
            raise RuntimeError("redis down")

    cleaner = LogCleaner(_Boom())  # type: ignore[arg-type]
    removed = await cleaner.sweep_once()
    assert removed == 0
    assert cleaner.stats.errors == 1
    assert cleaner.stats.last_error is not None
    assert "redis down" in cleaner.stats.last_error


async def test_background_loop_sweeps_then_stops(broker: Broker) -> None:
    await broker.create_topic(TopicConfig.compacted("c", partitions=1))
    producer = Producer(broker)
    await producer.send_and_wait(ProducerRecord("c", value=b"1", key=b"a", partition=0))
    await producer.send_and_wait(ProducerRecord("c", value=b"2", key=b"a", partition=0))
    await producer.close()

    cleaner = LogCleaner(broker, interval_s=0.01, clock=lambda: 10_000)
    async with cleaner:
        assert cleaner.running
        # Wait for at least one sweep to land.
        for _ in range(100):
            if cleaner.stats.sweeps >= 1:
                break
            await asyncio.sleep(0.01)
    assert not cleaner.running
    assert cleaner.stats.sweeps >= 1
    # The compaction actually happened.
    tp = TopicPartition("c", 0)
    assert (await broker.end_offsets((tp,)))[tp] == 2  # offsets preserved
    start = (await broker.beginning_offsets((tp,)))[tp]
    recs = (await broker.fetch("c", 0, start)).records
    assert [r.value for r in recs] == [b"2"]


async def test_start_is_idempotent(broker: Broker) -> None:
    cleaner = LogCleaner(broker, interval_s=10)
    cleaner.start()
    task = cleaner._task
    cleaner.start()  # no-op while running
    assert cleaner._task is task
    await cleaner.stop()
    await cleaner.stop()  # idempotent
