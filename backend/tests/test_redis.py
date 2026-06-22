"""Redis JSON / distributed-lock / pub-sub tests against a throwaway Redis.

SKIPs cleanly unless ``KINORA_TEST_REDIS_URL`` is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.redis.client import RedisClient

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping redis test"
)


async def test_redis_json_helpers() -> None:
    client = RedisClient.from_url(os.environ["KINORA_TEST_REDIS_URL"])
    try:
        assert await client.ping() is True
        key = f"kinora:test:json:{uuid.uuid4().hex}"
        await client.set_json(key, {"focus_word": 42, "velocity": 3.5}, ttl_s=30)
        assert await client.get_json(key) == {"focus_word": 42, "velocity": 3.5}
        assert await client.delete(key) == 1
        assert await client.get_json(key) is None
    finally:
        await client.close()


async def test_redis_distributed_lock() -> None:
    client = RedisClient.from_url(os.environ["KINORA_TEST_REDIS_URL"])
    try:
        name = f"kinora:test:lock:{uuid.uuid4().hex}"

        # Acquire; a second holder cannot take it; release; then it is free again.
        lock1 = client.lock(name, ttl_ms=5000, blocking=False)
        assert await lock1.acquire() is True
        lock2 = client.lock(name, ttl_ms=5000, blocking=False)
        assert await lock2.acquire() is False

        # A non-owner release must NOT free someone else's lock (token mismatch).
        assert await lock2.release() is False
        assert await lock1.release() is True

        # Now acquirable again.
        assert await lock2.acquire() is True
        await lock2.release()

        # Context-managed acquire/release.
        async with client.lock(name, ttl_ms=5000) as held:
            assert held.acquired is True
            contender = client.lock(name, ttl_ms=5000, blocking=False)
            assert await contender.acquire() is False
        # Released on context exit.
        freed = client.lock(name, blocking=False)
        assert await freed.acquire() is True
        await freed.release()
    finally:
        await client.close()


async def test_redis_pubsub() -> None:
    client = RedisClient.from_url(os.environ["KINORA_TEST_REDIS_URL"])
    try:
        channel = f"kinora:test:chan:{uuid.uuid4().hex}"
        message = {"event": "clip_ready", "shot_id": "shot_00042"}
        async with client.subscribe(channel) as pubsub:
            await asyncio.sleep(0.1)  # let the subscription register
            subscribers = await client.publish(channel, message)
            assert subscribers >= 1
            received = await client.next_message(pubsub, timeout=5.0)
            assert received == message
    finally:
        await client.close()
