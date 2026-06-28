"""Self-tests for the in-process Redis double (app.queue.fakeredis).

The double is the foundation the whole queue/worker unit suite stands on, so it
gets its own tests: the data-structure commands, lazy TTL expiry, the Lua-script
fingerprint guard, ``scan`` for namespace purge, and the JSON + pub/sub client
surface. These run with **no infra**.
"""

from __future__ import annotations

import pytest

from app.queue.fakeredis import (
    FakeAsyncRedis,
    FakeRedisClient,
    UnknownScriptError,
    UnsupportedCommandError,
)


async def test_string_set_get_incr_delete() -> None:
    r = FakeAsyncRedis()
    assert await r.get("k") is None
    await r.set("k", "v")
    assert await r.get("k") == "v"
    assert await r.incr("n") == 1
    assert await r.incr("n", 4) == 5
    assert await r.delete("k", "n") == 2
    assert await r.get("k") is None


async def test_set_nx_only_first_wins() -> None:
    r = FakeAsyncRedis()
    assert await r.set("lock", "a", nx=True) is True
    assert await r.set("lock", "b", nx=True) is None
    assert await r.get("lock") == "a"


async def test_hash_roundtrip_and_mapping() -> None:
    r = FakeAsyncRedis()
    await r.hset("h", "a", 1)
    await r.hset("h", mapping={"b": 2, "c": "x"})
    assert await r.hget("h", "a") == "1"
    assert await r.hgetall("h") == {"a": "1", "b": "2", "c": "x"}
    assert await r.hget("missing", "a") is None


async def test_set_membership() -> None:
    r = FakeAsyncRedis()
    assert await r.sadd("s", "x", "y", "x") == 2
    assert await r.scard("s") == 2
    assert await r.smembers("s") == {"x", "y"}
    assert await r.srem("s", "x", "z") == 1
    assert await r.smembers("s") == {"y"}


async def test_zset_order_and_range() -> None:
    r = FakeAsyncRedis()
    await r.zadd("z", {"a": 30, "b": 10, "c": 20})
    assert await r.zcard("z") == 3
    assert await r.zscore("z", "b") == 10
    # Range by score honours the (score, member) ordering and the LIMIT clause.
    assert await r.zrangebyscore("z", "-inf", 25) == ["b", "c"]
    assert await r.zrangebyscore("z", "-inf", "+inf", start=0, num=1) == ["b"]
    assert await r.zrem("z", "b") == 1
    assert await r.zrangebyscore("z", "-inf", "+inf") == ["c", "a"]


async def test_list_push_len_lrem() -> None:
    r = FakeAsyncRedis()
    await r.lpush("l", "a")
    await r.lpush("l", "b")  # b is now at head
    assert await r.lrange("l", 0, -1) == ["b", "a"]
    assert await r.llen("l") == 2
    await r.rpush("l", "a")
    assert await r.lrem("l", 0, "a") == 2  # remove every 'a'
    assert await r.lrange("l", 0, -1) == ["b"]


async def test_lazy_ttl_expiry() -> None:
    # Drive a deterministic clock so expiry is testable without sleeping.
    r = FakeAsyncRedis()
    t = {"now": 1000.0}
    r._clock = lambda: t["now"]
    await r.set("k", "v", ex=10)
    assert await r.get("k") == "v"
    t["now"] = 1009.0
    assert await r.get("k") == "v"  # not yet expired
    t["now"] = 1011.0
    assert await r.get("k") is None  # lazily expired on read


async def test_expire_only_on_existing_key() -> None:
    r = FakeAsyncRedis()
    assert await r.expire("ghost", 10) is False
    await r.set("k", "v")
    assert await r.expire("k", 10) is True


async def test_scan_matches_pattern() -> None:
    r = FakeAsyncRedis()
    await r.set("ns:a", "1")
    await r.hset("ns:b", "f", "1")
    await r.set("other:c", "1")
    cursor, keys = await r.scan(match="ns:*")
    assert cursor == 0
    assert set(keys) == {"ns:a", "ns:b"}


async def test_unsupported_command_raises() -> None:
    r = FakeAsyncRedis()
    with pytest.raises(UnsupportedCommandError):
        await r.getset("k", "v")


async def test_unknown_lua_script_raises() -> None:
    r = FakeAsyncRedis()
    with pytest.raises(UnknownScriptError):
        await r.eval("return redis.call('GET', KEYS[1])", 1, "k")


async def test_client_json_roundtrip_and_pubsub() -> None:
    client = FakeRedisClient()
    await client.set_json("k", {"a": 1})
    assert await client.get_json("k") == {"a": 1}

    async with client.subscribe("chan") as ps:
        await client.publish("chan", {"event": "hello"})
        msg = await client.next_message(ps, timeout=1.0)
    assert msg == {"event": "hello"}
    assert client.events_on("chan") == [{"event": "hello"}]


async def test_client_pubsub_timeout_returns_none() -> None:
    client = FakeRedisClient()
    async with client.subscribe("quiet") as ps:
        assert await client.next_message(ps, timeout=0.01) is None


async def test_client_lock_is_owner_scoped() -> None:
    client = FakeRedisClient()
    async with client.lock("res") as lk:
        assert lk.acquired
        other = client.lock("res")
        assert await other.acquire() is False  # held
    # released on exit
    again = client.lock("res")
    assert await again.acquire() is True
