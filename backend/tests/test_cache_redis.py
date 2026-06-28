"""Tests for the Redis (L2) backend.

The envelope (wire format) tests are infra-free. The live backend tests require
an isolated test Redis (``KINORA_TEST_REDIS_URL``, by convention redis db 15)
and skip cleanly when it is not configured — exactly like the rest of the
integration suite.

Live tests use a unique per-test key prefix so they never collide with the
render queue / pubsub keys on a shared instance, and clean up after themselves.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.cache.cache import CacheConfig
from app.cache.clock import FakeClock
from app.cache.codecs import JsonCodec, PickleCodec
from app.cache.entry import CacheEntry
from app.cache.errors import SerializationError
from app.cache.factory import redis_cache, tiered_cache
from app.cache.metrics import CacheMetrics
from app.cache.redis_backend import RedisCache, decode_envelope, encode_envelope

# --------------------------------------------------------------------------- #
# Envelope (infra-free)
# --------------------------------------------------------------------------- #


def test_envelope_roundtrip_positive() -> None:
    codec = JsonCodec()
    entry = CacheEntry.of(
        {"x": [1, 2, 3]}, now=100.0, ttl=60.0, tags=frozenset({"a", "b"}), codec="json"
    )
    blob = encode_envelope(entry, codec)
    back = decode_envelope(blob, codec)
    assert back.value == {"x": [1, 2, 3]}
    assert back.created_at == 100.0
    assert back.expires_at == 160.0
    assert back.ttl == 60.0
    assert back.tags == frozenset({"a", "b"})
    assert not back.negative


def test_envelope_roundtrip_negative_has_empty_payload() -> None:
    codec = JsonCodec()
    entry = CacheEntry.of(None, now=10.0, ttl=5.0, negative=True)
    blob = encode_envelope(entry, codec)
    back = decode_envelope(blob, codec)
    assert back.negative
    assert back.value is None


def test_envelope_roundtrip_no_ttl() -> None:
    codec = PickleCodec()
    entry = CacheEntry.of(("tuple", 1), now=0.0, ttl=None)
    back = decode_envelope(encode_envelope(entry, codec), codec)
    assert back.value == ("tuple", 1)
    assert back.expires_at is None
    assert back.ttl is None


def test_envelope_rejects_truncated() -> None:
    with pytest.raises(SerializationError):
        decode_envelope(b"\x01\x00", JsonCodec())


def test_envelope_rejects_bad_version() -> None:
    codec = JsonCodec()
    blob = bytearray(encode_envelope(CacheEntry.of(1, now=0.0), codec))
    blob[0] = 0xFE  # corrupt the version byte
    with pytest.raises(SerializationError):
        decode_envelope(bytes(blob), codec)


# --------------------------------------------------------------------------- #
# Live Redis (skips without KINORA_TEST_REDIS_URL)
# --------------------------------------------------------------------------- #

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")


def requires_redis(fn: object) -> object:
    """Mark a live Redis test: needs the test URL *and* an event loop."""
    skipped = pytest.mark.skipif(
        not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping Redis cache integration"
    )(fn)
    return pytest.mark.asyncio(skipped)


@pytest_asyncio.fixture
async def redis_binary() -> AsyncIterator[object]:
    """A binary-mode async Redis client scoped to a unique prefix per test."""
    assert _REDIS_URL is not None
    from redis.asyncio import Redis

    client = Redis.from_url(_REDIS_URL, decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()  # type: ignore[attr-defined]


def _unique_prefix() -> str:
    return f"kinora:test:cache:{uuid.uuid4().hex}"


@requires_redis
async def test_redis_backend_set_get(redis_binary: object) -> None:
    clk = FakeClock()
    backend = RedisCache(redis_binary, prefix=_unique_prefix(), clock=clk)
    try:
        await backend.set("k", CacheEntry.of({"v": 1}, now=clk.time(), ttl=60.0))
        entry = await backend.get("k")
        assert entry is not None and entry.value == {"v": 1}
        assert await backend.get("missing") is None
    finally:
        await backend.clear()


@requires_redis
async def test_redis_backend_delete_and_tag(redis_binary: object) -> None:
    clk = FakeClock()
    backend = RedisCache(redis_binary, prefix=_unique_prefix(), clock=clk)
    try:
        await backend.set("a", CacheEntry.of(1, now=clk.time(), tags=frozenset({"t"})))
        await backend.set("b", CacheEntry.of(2, now=clk.time(), tags=frozenset({"t"})))
        assert await backend.delete("a") is True
        assert await backend.get("a") is None
        removed = await backend.delete_tag("t")
        # "a" was already gone; "b" remains tagged -> at least one removed.
        assert removed >= 1
        assert await backend.get("b") is None
    finally:
        await backend.clear()


@requires_redis
async def test_redis_facade_get_or_load(redis_binary: object) -> None:
    clk = FakeClock()
    m = CacheMetrics()
    cache = redis_cache(
        redis_binary,
        namespace="ns",
        prefix=_unique_prefix(),
        clock=clk,
        metrics=m,
        config=CacheConfig(namespace="ns", default_ttl=60.0),
    )
    try:
        calls = 0

        async def loader() -> int:
            nonlocal calls
            calls += 1
            return 5

        assert await cache.get_or_load("k", loader) == 5
        assert await cache.get_or_load("k", loader) == 5
        assert calls == 1
        await cache.invalidate("k")
        assert await cache.get_or_load("k", loader) == 5
        assert calls == 2
    finally:
        await cache.invalidate_namespace()


@requires_redis
async def test_redis_tiered_survives_l1_eviction(redis_binary: object) -> None:
    clk = FakeClock()
    cache = tiered_cache(
        redis_binary,
        namespace="ns2",
        prefix=_unique_prefix(),
        l1_max_entries=1,  # tiny L1 so writes evict from L1 but persist in L2
        clock=clk,
        config=CacheConfig(namespace="ns2", default_ttl=120.0),
    )
    try:
        await cache.set("a", "va")
        await cache.set("b", "vb")  # evicts "a" from L1
        # "a" is gone from L1 but still in L2 -> read promotes it back.
        assert await cache.get("a") == "va"
    finally:
        await cache.invalidate_namespace()


@requires_redis
async def test_redis_native_ttl_expiry(redis_binary: object) -> None:
    # A near-zero TTL should expire promptly in Redis itself.
    backend = RedisCache(redis_binary, prefix=_unique_prefix())
    try:
        import time as _t

        now = _t.time()
        await backend.set("k", CacheEntry.of("v", now=now, ttl=0.05))
        await _sleep(0.2)
        assert await backend.get("k") is None
    finally:
        await backend.clear()


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
