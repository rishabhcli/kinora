"""Metrics tests — sink protocol, snapshot aggregation, broker emission."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.metrics import InMemoryMetrics, NullMetrics
from app.streaming.log.producer import Producer
from app.streaming.log.record import ProducerRecord, TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


def test_in_memory_metrics_counters_and_labels() -> None:
    m = InMemoryMetrics()
    m.incr("records_produced", topic="a")
    m.incr("records_produced", 2, topic="a")
    m.incr("records_produced", topic="b")
    snap = m.snapshot()
    assert snap.counter("records_produced", topic="a") == 3
    assert snap.counter("records_produced", topic="b") == 1
    assert snap.counter("records_produced") == 0  # unlabelled is distinct


def test_in_memory_metrics_observations_mean() -> None:
    m = InMemoryMetrics()
    m.observe("fetch_batch_size", 2.0)
    m.observe("fetch_batch_size", 4.0)
    assert m.snapshot().mean("fetch_batch_size") == 3.0
    assert m.snapshot().mean("never_seen") == 0.0


def test_null_metrics_is_noop() -> None:
    m = NullMetrics()
    m.incr("x")
    m.observe("y", 1.0)  # must not raise


def test_metrics_reset() -> None:
    m = InMemoryMetrics()
    m.incr("x")
    m.reset()
    assert m.snapshot().counter("x") == 0


@pytest.fixture(params=["memory", "redis"])
async def broker_metrics(request: pytest.FixtureRequest) -> tuple[Broker, InMemoryMetrics]:
    metrics = InMemoryMetrics()
    impl: Broker = (
        InMemoryBroker(metrics=metrics)
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="m", metrics=metrics)
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("beats", partitions=2))
    return impl, metrics


async def test_broker_emits_produce_fetch_commit_rebalance(
    broker_metrics: tuple[Broker, InMemoryMetrics],
) -> None:
    broker, metrics = broker_metrics
    producer = Producer(broker)
    for i in range(5):
        await producer.send_and_wait(ProducerRecord("beats", value=bytes([i]), partition=0))
    await producer.close()

    consumer = Consumer(broker, config=ConsumerConfig(group_id="g"))
    await consumer.subscribe(("beats",))  # → rebalance
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll()
    await consumer.commit()

    snap = metrics.snapshot()
    assert snap.counter("records_produced", topic="beats") == 5
    assert snap.counter("records_fetched", topic="beats") == 5
    assert snap.counter("fetch_requests", topic="beats") >= 1
    assert snap.counter("offset_commits", group="g") == 1
    assert snap.counter("rebalances", group="g") >= 1


async def test_broker_emits_cleaned_and_dedup(
    broker_metrics: tuple[Broker, InMemoryMetrics],
) -> None:
    broker, metrics = broker_metrics
    await broker.create_topic(TopicConfig.compacted("c", partitions=1))
    producer = Producer(broker)
    for v in (b"1", b"2", b"3"):
        await producer.send_and_wait(ProducerRecord("c", value=v, key=b"a", partition=0))
    await producer.close()

    await broker.maintain(now=10_000)
    assert metrics.snapshot().counter("records_cleaned", topic="c") == 2
