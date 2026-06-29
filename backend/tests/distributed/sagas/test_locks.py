"""Distributed locks / leases with fencing tokens (in-memory reference)."""

from __future__ import annotations

import pytest

from app.distributed.sagas.locks import (
    FencedResource,
    InMemoryLockManager,
    LockAcquireTimeout,
    StaleFenceError,
)
from app.jobs.clock import ManualClock


async def test_mutual_exclusion() -> None:
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    a = await mgr.acquire("canon:book_1", ttl_s=30)
    assert a is not None
    # A second contender cannot acquire while the first holds it.
    b = await mgr.acquire("canon:book_1", ttl_s=30)
    assert b is None
    assert await mgr.release(a) is True
    # Now it's free.
    c = await mgr.acquire("canon:book_1", ttl_s=30)
    assert c is not None


async def test_fencing_tokens_are_monotonic() -> None:
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    a = await mgr.acquire("r", ttl_s=10)
    assert a is not None
    await mgr.release(a)
    b = await mgr.acquire("r", ttl_s=10)
    assert b is not None
    # Even across release+reacquire the token strictly increases.
    assert b.fence > a.fence


async def test_expired_lease_can_be_taken_and_old_holder_is_fenced() -> None:
    """The classic fencing scenario: a stalled holder's lease lapses and is taken.

    Holder A's lease expires; holder B acquires with a higher fencing token and
    writes to a :class:`FencedResource`. When the stalled A wakes and tries to
    write with its now-stale token, the resource rejects it — A is fenced off.
    """
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    resource = FencedResource("canon:book_1")

    a = await mgr.acquire("canon:book_1", ttl_s=10)
    assert a is not None
    await resource.guard(a.fence)  # A writes happily under its lease

    # A stalls; its 10s lease lapses.
    await clock.advance(11)
    b = await mgr.acquire("canon:book_1", ttl_s=10)
    assert b is not None
    assert b.fence > a.fence
    await resource.guard(b.fence)  # B writes with the newer token

    # A wakes up and tries to write with its stale token → fenced off.
    with pytest.raises(StaleFenceError):
        await resource.guard(a.fence)


async def test_renew_extends_only_for_owner() -> None:
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    a = await mgr.acquire("r", owner="A", ttl_s=10)
    assert a is not None
    await clock.advance(5)
    renewed = await mgr.renew(a, ttl_s=10)
    assert renewed is not None
    assert renewed.expires_at > a.expires_at
    # After expiry without renew, another owner takes it; the old lease can't renew.
    await clock.advance(20)
    b = await mgr.acquire("r", owner="B", ttl_s=10)
    assert b is not None
    assert await mgr.renew(a, ttl_s=10) is None  # A lost it


async def test_acquire_blocking_times_out() -> None:
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    held = await mgr.acquire("r", ttl_s=100)
    assert held is not None

    import asyncio

    task = asyncio.create_task(
        mgr.acquire_blocking("r", ttl_s=10, wait_s=5, poll_s=1)
    )
    # Advance past the wait budget without ever releasing.
    for _ in range(20):
        await asyncio.sleep(0)
        await clock.advance(1)
    with pytest.raises(LockAcquireTimeout):
        await task


async def test_acquire_blocking_succeeds_when_freed() -> None:
    clock = ManualClock()
    mgr = InMemoryLockManager(clock=clock)
    held = await mgr.acquire("r", ttl_s=2)
    assert held is not None

    import asyncio

    task = asyncio.create_task(mgr.acquire_blocking("r", ttl_s=10, wait_s=30, poll_s=1))
    # The 2s lease lapses; the blocking acquirer then wins on its next poll.
    for _ in range(10):
        await asyncio.sleep(0)
        await clock.advance(1)
    lease = await task
    assert lease.fence > held.fence


async def test_fenced_resource_accepts_same_token_repeatedly() -> None:
    """A single holder may issue several writes under one lease (>= not strict >)."""
    resource = FencedResource("r")
    await resource.guard(5)
    await resource.guard(5)  # same token, same holder — fine
    await resource.guard(6)
    with pytest.raises(StaleFenceError):
        await resource.guard(5)  # an older token is now stale
    assert resource.highest_fence == 6
