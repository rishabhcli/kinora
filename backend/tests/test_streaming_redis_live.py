"""Live Redis-adapter wiring test for the streams broker.

The broker's *logic* is covered exhaustively against the in-process Redis double
in the other ``test_streaming_*`` modules. This module confirms the
:class:`RedisStreamAdapter` actually speaks to a real Redis the way the broker
expects — the one thing the fake cannot prove.

SKIPs cleanly unless ``KINORA_TEST_REDIS_URL`` is set. Each test uses a unique
namespace and deletes its keys on teardown, so it never touches the live kinora
data (run it against an isolated db, e.g. redis db 15).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.producer import Producer, ProducerConfig
from app.streaming.log.record import ProducerRecord, TopicPartition
from app.streaming.log.redis import RedisStreamAdapter, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping live streams test"
)


@pytest_asyncio.fixture
async def broker() -> AsyncIterator[RedisStreamsBroker]:
    assert _REDIS_URL is not None
    adapter = RedisStreamAdapter.from_url(_REDIS_URL)
    ns = f"kinora:test:stream:{uuid.uuid4().hex[:10]}"
    b = RedisStreamsBroker(adapter, namespace=ns)
    await b.start()
    try:
        yield b
    finally:
        keys = await adapter.keys(f"{ns}:*")
        if keys:
            await adapter.delete(*keys)
        await adapter.aclose()


async def test_produce_consume_commit_against_real_redis(broker: RedisStreamsBroker) -> None:
    await broker.create_topic(TopicConfig.deleted("beats", partitions=2))
    producer = Producer(broker, config=ProducerConfig(enable_idempotence=True))
    for i in range(6):
        await producer.send_and_wait(ProducerRecord.from_json("beats", {"i": i}, key="book-7"))
    await producer.close()

    consumer = Consumer(broker, config=ConsumerConfig(group_id="renderers"))
    await consumer.subscribe(("beats",))
    seen: list[int] = []
    for _ in range(4):
        batch = await consumer.poll()
        seen.extend(r.json()["i"] for r in batch)
        if len(seen) >= 6:
            break
    assert sorted(seen) == list(range(6))
    await consumer.commit()
    await consumer.close()


async def test_offsets_and_retention_against_real_redis(broker: RedisStreamsBroker) -> None:
    await broker.create_topic(TopicConfig.deleted("t", partitions=1, retention_ms=0))
    for i in range(5):
        await broker.produce("t", 0, key=None, value=bytes([i]), timestamp_ms=i)
    tp = TopicPartition("t", 0)
    assert (await broker.end_offsets((tp,)))[tp] == 5
    removed = await broker.maintain(now=10_000)
    assert removed >= 1
    # The newest record is always retained and still fetchable at its offset.
    start = (await broker.beginning_offsets((tp,)))[tp]
    tail = (await broker.fetch("t", 0, start)).records
    assert tail[-1].offset == 4
