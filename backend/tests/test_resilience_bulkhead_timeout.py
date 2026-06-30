"""Tests for the bulkhead concurrency limiter and the timeout wrapper."""

from __future__ import annotations

import asyncio

import pytest

from app.resilience.bulkhead import Bulkhead, BulkheadConfig
from app.resilience.errors import BulkheadFull, CallTimeout
from app.resilience.timeout import call_with_timeout, timeout

# --------------------------------------------------------------------------- #
# Bulkhead
# --------------------------------------------------------------------------- #


async def test_admits_up_to_max_concurrency() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=2, max_queue=0))
    release = asyncio.Event()

    async def hold() -> str:
        async with bh.slot():
            await release.wait()
            return "done"

    t1 = asyncio.create_task(hold())
    t2 = asyncio.create_task(hold())
    await asyncio.sleep(0)  # let both grab slots
    assert bh.active == 2
    release.set()
    assert await t1 == "done"
    assert await t2 == "done"
    assert bh.active == 0


async def test_sheds_immediately_when_full_and_no_queue() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1, max_queue=0))
    release = asyncio.Event()

    async def hold() -> None:
        async with bh.slot():
            await release.wait()

    t1 = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert bh.active == 1
    with pytest.raises(BulkheadFull) as ei:
        async with bh.slot():
            pass
    assert ei.value.name == "dep"
    assert bh.snapshot().total_rejected == 1
    release.set()
    await t1


async def test_queue_admits_then_releases() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1, max_queue=2))
    order: list[int] = []
    gate = asyncio.Event()

    async def worker(i: int) -> None:
        async with bh.slot():
            order.append(i)
            if i == 0:
                await gate.wait()

    t0 = asyncio.create_task(worker(0))
    await asyncio.sleep(0)
    assert bh.active == 1
    t1 = asyncio.create_task(worker(1))
    await asyncio.sleep(0)
    assert bh.waiting == 1  # queued behind t0
    gate.set()
    await asyncio.gather(t0, t1)
    assert order == [0, 1]
    assert bh.active == 0


async def test_acquire_timeout_sheds_queued_waiter() -> None:
    bh = Bulkhead(
        "dep", BulkheadConfig(max_concurrency=1, max_queue=5, acquire_timeout_s=0.05)
    )
    release = asyncio.Event()

    async def hold() -> None:
        async with bh.slot():
            await release.wait()

    t1 = asyncio.create_task(hold())
    await asyncio.sleep(0)
    with pytest.raises(BulkheadFull):
        async with bh.slot():
            pass
    release.set()
    await t1


async def test_run_helper_holds_slot() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1))

    async def work() -> int:
        return 7

    assert await bh.run(work()) == 7
    assert bh.active == 0


async def test_slot_released_on_exception() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1, max_queue=0))
    with pytest.raises(RuntimeError):
        async with bh.slot():
            raise RuntimeError("boom")
    assert bh.active == 0
    # Slot is reusable.
    async with bh.slot():
        assert bh.active == 1


async def test_fifo_transfer_does_not_leak_slots() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1, max_queue=5))
    gate = asyncio.Event()
    finished: list[int] = []

    async def worker(i: int) -> None:
        async with bh.slot():
            if i == 0:
                await gate.wait()
            finished.append(i)

    t0 = asyncio.create_task(worker(0))
    await asyncio.sleep(0)
    t1 = asyncio.create_task(worker(1))
    t2 = asyncio.create_task(worker(2))
    await asyncio.sleep(0)
    assert bh.waiting == 2
    gate.set()
    await asyncio.gather(t0, t1, t2)
    assert finished == [0, 1, 2]  # FIFO order preserved
    assert bh.active == 0  # no slot leaked


async def test_timeout_does_not_leak_when_others_wait() -> None:
    # A waiter that times out must not permanently consume the (single) slot:
    # after the holder releases, a fresh acquire still succeeds.
    bh = Bulkhead(
        "dep", BulkheadConfig(max_concurrency=1, max_queue=5, acquire_timeout_s=0.02)
    )
    release = asyncio.Event()

    async def hold() -> None:
        async with bh.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    with pytest.raises(BulkheadFull):
        async with bh.slot():
            pass
    release.set()
    await holder
    assert bh.active == 0
    # Slot is healthy and reusable.
    async with bh.slot():
        assert bh.active == 1
    assert bh.active == 0


def test_bulkhead_config_validation() -> None:
    with pytest.raises(ValueError):
        BulkheadConfig(max_concurrency=0)
    with pytest.raises(ValueError):
        BulkheadConfig(max_queue=-1)
    with pytest.raises(ValueError):
        BulkheadConfig(acquire_timeout_s=0.0)


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #


async def test_timeout_passthrough_when_none() -> None:
    async def quick() -> int:
        return 5

    assert await call_with_timeout(quick(), None) == 5


async def test_fast_call_does_not_time_out() -> None:
    async def quick() -> str:
        await asyncio.sleep(0)
        return "ok"

    assert await call_with_timeout(quick(), 1.0) == "ok"


async def test_slow_call_raises_call_timeout() -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()  # never resolves

    with pytest.raises(CallTimeout):
        await call_with_timeout(hang(), 0.05, name="hang")
    assert started.is_set()


async def test_timeout_decorator() -> None:
    @timeout(0.05, name="slow")
    async def slow() -> None:
        await asyncio.Event().wait()

    with pytest.raises(CallTimeout):
        await slow()


async def test_timeout_rejects_nonpositive() -> None:
    async def q() -> int:
        return 1

    coro = q()
    with pytest.raises(ValueError):
        await call_with_timeout(coro, 0.0)
    coro.close()  # we never awaited it (ValueError raised before await)
