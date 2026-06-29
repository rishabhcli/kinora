"""Tests for the connection proxy / pooler (no infra; deterministic fakes)."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from app.datascale.sharding.proxy import (
    BackendConnection,
    BackendFactory,
    ConnectionProxy,
    PoolClosed,
    PoolExhausted,
    PoolTimeout,
    ProxyConfig,
    ShardProxyPool,
)

pytestmark = pytest.mark.asyncio

_ids = itertools.count(1)


class FakeBackend:
    """A scriptable in-memory backend connection recording its lifecycle."""

    def __init__(self, *, alive: bool = True) -> None:
        self._id = next(_ids)
        self.alive = alive
        self.events: list[str] = []
        self.closed = False

    @property
    def id(self) -> int:
        return self._id

    async def ping(self) -> bool:
        return self.alive

    async def begin(self) -> None:
        self.events.append("begin")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.closed = True
        self.events.append("close")


def _counting_factory() -> tuple[list[FakeBackend], BackendFactory]:
    """A factory that records every backend it creates."""
    created: list[FakeBackend] = []

    async def factory() -> BackendConnection:
        b = FakeBackend()
        created.append(b)
        return b

    return created, factory


async def test_transaction_brackets_begin_commit_and_releases() -> None:
    created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=2))
    async with proxy.transaction():
        assert proxy.stats().checked_out == 1
    # After the block: committed and returned to idle.
    assert created[0].events == ["begin", "commit"]
    assert proxy.stats().checked_out == 0
    assert proxy.stats().idle == 1


async def test_transaction_rolls_back_on_error_and_still_releases() -> None:
    created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=1))
    with pytest.raises(ValueError):
        async with proxy.transaction():
            raise ValueError("boom")
    assert created[0].events == ["begin", "rollback"]
    # Backend is back in the pool (clean rollback ⇒ reusable).
    assert proxy.stats().idle == 1
    assert proxy.stats().checked_out == 0


async def test_transaction_pooling_reuses_one_backend_serially() -> None:
    created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=4))
    for _ in range(10):
        async with proxy.transaction():
            pass
    # Ten sequential transactions reuse a single backend (transaction pooling).
    assert len(created) == 1
    assert proxy.stats().total_acquired == 10


async def test_multiplexing_caps_backends_at_pool_size() -> None:
    created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=3, acquire_timeout_s=2.0))

    started = asyncio.Event()
    release = asyncio.Event()
    holding = 0

    async def client() -> None:
        nonlocal holding
        async with proxy.transaction():
            holding += 1
            if holding >= 3:
                started.set()
            await release.wait()

    # 8 logical clients, pool of 3: only 3 backends ever get opened.
    tasks = [asyncio.create_task(client()) for _ in range(8)]
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert proxy.stats().checked_out == 3
    assert proxy.stats().open_backends == 3
    assert proxy.stats().waiters == 5  # the rest are queued
    release.set()
    await asyncio.gather(*tasks)
    assert len(created) == 3  # never exceeded pool_size


async def test_acquire_timeout_when_pool_stays_full() -> None:
    _created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=1, acquire_timeout_s=0.05))
    held = await proxy.acquire()
    try:
        with pytest.raises(PoolTimeout):
            await proxy.acquire()
        assert proxy.stats().total_timeouts == 1
    finally:
        await proxy.release(held)


async def test_wait_queue_fail_fast_when_full() -> None:
    _created, factory = _counting_factory()
    proxy = ConnectionProxy(
        factory, ProxyConfig(pool_size=1, max_waiters=1, acquire_timeout_s=1.0)
    )
    held = await proxy.acquire()
    # First waiter queues; second exceeds max_waiters ⇒ PoolExhausted immediately.
    waiter = asyncio.create_task(proxy.acquire())
    await asyncio.sleep(0.02)  # let the first waiter enqueue
    assert proxy.stats().waiters == 1
    with pytest.raises(PoolExhausted):
        await proxy.acquire()
    # Clean up: release frees the queued waiter.
    await proxy.release(held)
    freed = await asyncio.wait_for(waiter, timeout=1.0)
    await proxy.release(freed)


async def test_fifo_fairness_first_waiter_served_first() -> None:
    _created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=1, acquire_timeout_s=2.0))
    held = await proxy.acquire()
    order: list[int] = []

    async def waiter(tag: int) -> None:
        conn = await proxy.acquire()
        order.append(tag)
        await asyncio.sleep(0.01)
        await proxy.release(conn)

    w1 = asyncio.create_task(waiter(1))
    await asyncio.sleep(0.01)
    w2 = asyncio.create_task(waiter(2))
    await asyncio.sleep(0.01)
    w3 = asyncio.create_task(waiter(3))
    await asyncio.sleep(0.01)
    await proxy.release(held)  # wake the first waiter
    await asyncio.gather(w1, w2, w3)
    assert order == [1, 2, 3]  # served in arrival order


async def test_unhealthy_backend_discarded_and_replaced() -> None:
    created: list[FakeBackend] = []

    async def factory() -> BackendConnection:
        # First backend is dead; the replacement is alive.
        b = FakeBackend(alive=len(created) >= 1)
        created.append(b)
        return b

    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=1, pre_ping=True))
    # Pre-seed: open and release the dead backend so it sits idle.
    conn = await proxy.acquire()
    await proxy.release(conn)
    # Next acquire pre-pings, finds it dead, discards + opens a fresh one.
    conn2 = await proxy.acquire()
    assert proxy.stats().total_health_discards == 1
    assert created[0].closed is True  # the dead one was closed
    assert conn2 is created[1]  # got the healthy replacement
    await proxy.release(conn2)


async def test_recycle_on_age() -> None:
    created, factory = _counting_factory()
    # max_lifetime_s tiny so the backend is "expired" on the next acquire.
    proxy = ConnectionProxy(
        factory, ProxyConfig(pool_size=1, max_lifetime_s=0.001, pre_ping=False)
    )
    conn = await proxy.acquire()
    await proxy.release(conn)
    await asyncio.sleep(0.005)  # age past max_lifetime
    conn2 = await proxy.acquire()
    assert proxy.stats().total_recycled == 1
    assert created[0].closed is True  # old backend recycled (closed)
    assert conn2 is created[1]
    await proxy.release(conn2)


async def test_close_rejects_new_acquires_and_wakes_waiters() -> None:
    _created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=1, acquire_timeout_s=2.0))
    held = await proxy.acquire()
    waiter = asyncio.create_task(proxy.acquire())
    await asyncio.sleep(0.02)
    await proxy.close()
    with pytest.raises(PoolClosed):
        await waiter
    with pytest.raises(PoolClosed):
        await proxy.acquire()
    # Releasing the still-held backend after close discards it.
    await proxy.release(held)


async def test_shard_proxy_pool_lazily_creates_per_shard() -> None:
    factories = {
        "s1": _counting_factory()[1],
        "s2": _counting_factory()[1],
    }
    pool = ShardProxyPool(factories=factories, config=ProxyConfig(pool_size=2))
    async with pool.transaction("s1"):
        pass
    async with pool.transaction("s2"):
        pass
    stats = pool.stats()
    assert set(stats.keys()) == {"s1", "s2"}
    assert stats["s1"].total_acquired == 1
    with pytest.raises(KeyError):
        pool.proxy_for("unknown")
    await pool.close()


async def test_config_validation() -> None:
    with pytest.raises(ValueError):
        ProxyConfig(pool_size=0)
    with pytest.raises(ValueError):
        ProxyConfig(max_waiters=-1)
    with pytest.raises(ValueError):
        ProxyConfig(acquire_timeout_s=0)
    with pytest.raises(ValueError):
        ProxyConfig(max_lifetime_s=-1)


async def test_stats_utilization_and_saturation() -> None:
    _created, factory = _counting_factory()
    proxy = ConnectionProxy(factory, ProxyConfig(pool_size=2))
    c1 = await proxy.acquire()
    assert proxy.stats().utilization == 0.5
    assert not proxy.stats().is_saturated
    c2 = await proxy.acquire()
    assert proxy.stats().is_saturated
    d = proxy.stats().as_dict()
    assert d["checked_out"] == 2 and d["pool_size"] == 2
    await proxy.release(c1)
    await proxy.release(c2)
