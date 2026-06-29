"""Prometheus metrics-adapter tests (skip cleanly if prometheus_client is absent)."""

from __future__ import annotations

import pytest

from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.producer import Producer
from app.streaming.log.record import ProducerRecord, TopicPartition
from app.streaming.log.topic import TopicConfig

pytest.importorskip("prometheus_client")

from app.streaming.log.prometheus import PrometheusMetrics  # noqa: E402


def _sample(registry: object, name: str, **labels: str) -> float:
    from prometheus_client import CollectorRegistry

    assert isinstance(registry, CollectorRegistry)
    value = registry.get_sample_value(name, labels or None)
    return value if value is not None else 0.0


def test_counter_and_summary_register_and_increment() -> None:
    metrics = PrometheusMetrics()
    metrics.incr("records_produced", 3, topic="beats")
    metrics.observe("fetch_batch_size", 5.0, topic="beats")
    assert _sample(metrics.registry, "kinora_streaming_records_produced_total", topic="beats") == 3
    count = _sample(metrics.registry, "kinora_streaming_fetch_batch_size_count", topic="beats")
    assert count == 1


def test_unlabelled_metric() -> None:
    metrics = PrometheusMetrics()
    metrics.incr("global_events", 2)
    assert _sample(metrics.registry, "kinora_streaming_global_events_total") == 2


def test_label_name_sanitisation() -> None:
    metrics = PrometheusMetrics()
    metrics.incr("weird-name", 1, **{"odd.label": "v"})
    # Hyphen/dot become underscores; the metric is still scrapeable.
    assert _sample(metrics.registry, "kinora_streaming_weird_name_total", odd_label="v") == 1


async def test_broker_emits_into_prometheus() -> None:
    metrics = PrometheusMetrics()
    broker = InMemoryBroker(metrics=metrics)
    await broker.start()
    await broker.create_topic(TopicConfig.deleted("beats", partitions=1))

    producer = Producer(broker)
    for i in range(4):
        await producer.send_and_wait(ProducerRecord("beats", value=bytes([i]), partition=0))
    await producer.close()

    consumer = Consumer(broker, config=ConsumerConfig(group_id="g"))
    await consumer.assign((TopicPartition("beats", 0),))
    await consumer.poll()
    await consumer.commit()

    assert _sample(metrics.registry, "kinora_streaming_records_produced_total", topic="beats") == 4
    assert _sample(metrics.registry, "kinora_streaming_offset_commits_total", group="g") == 1
