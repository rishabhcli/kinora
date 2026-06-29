"""Distributed locks / leases with fencing tokens.

A saga that mutates a shared resource (the canon for one book, the budget for one
session) must hold a **lease** so two concurrent saga instances don't interleave
their writes. A naive lock is not enough in a distributed system: a holder can
stall (GC pause, network partition) long enough that its lease expires and is
granted to someone else, then *wake up* and complete its now-stale write. The
defence is a **fencing token** — a monotonically increasing number minted on each
acquisition. Every protected write carries the token; the resource rejects any
write whose token is below the highest it has seen. The stalled old holder's
write is fenced off (Martin Kleppmann's canonical example).

This module provides:

* :class:`Lease` — the value object: resource name, owner token, fencing token,
  and the expiry instant.
* :class:`LockManager` — the protocol: ``acquire`` (with the fencing token),
  ``renew``, ``release``, and ``is_held``.
* :class:`InMemoryLockManager` — a single-process reference implementation used by
  the harness/tests (still issues real, monotonically increasing fencing tokens).
* :class:`RedisLockManager` — a cross-process implementation built on the same
  ``SET NX PX`` + Lua compare-and-renew / compare-and-delete pattern proven in
  :class:`app.jobs.lease.LeaderLease`, plus a per-resource ``INCR`` fence counter.
* :class:`FencedResource` — a tiny helper a protected resource can embed to
  enforce *monotonic fencing*: it remembers the highest token it has accepted and
  rejects stale ones, which is what actually makes fencing *do* anything.

Acquisition is non-blocking (try-once) plus a convenience ``acquire_blocking``
that retries against the injected :class:`~app.jobs.clock.Clock` so it stays
virtual-clock friendly in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from app.jobs.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class Lease:
    """A granted lease over a named resource, carrying its fencing token."""

    resource: str
    owner: str
    fence: int
    expires_at: datetime

    def is_valid_at(self, now: datetime) -> bool:
        """Whether the lease has not yet expired at ``now``."""
        return now < self.expires_at


class LockAcquireTimeout(TimeoutError):  # noqa: N818 - public name in the lock contract
    """Raised by :meth:`LockManager.acquire_blocking` when the wait budget elapses."""


class StaleFenceError(RuntimeError):
    """Raised by :meth:`FencedResource.guard` when a write presents a stale token."""

    def __init__(self, resource: str, presented: int, highest: int) -> None:
        super().__init__(
            f"stale fencing token for {resource!r}: presented {presented} < highest {highest}"
        )
        self.resource = resource
        self.presented = presented
        self.highest = highest


@runtime_checkable
class LockManager(Protocol):
    """Acquire / renew / release named leases with monotonic fencing tokens."""

    async def acquire(
        self, resource: str, *, owner: str | None = None, ttl_s: float = 30.0
    ) -> Lease | None:
        """Try once to acquire ``resource``; return a :class:`Lease` or ``None``."""
        ...

    async def renew(self, lease: Lease, *, ttl_s: float = 30.0) -> Lease | None:
        """Extend ``lease`` iff still owned; return the refreshed lease or ``None``."""
        ...

    async def release(self, lease: Lease) -> bool:
        """Release ``lease`` iff still owned (owner-only). Returns whether it was held."""
        ...

    async def is_held(self, resource: str) -> bool:
        """Whether anyone currently holds ``resource``."""
        ...


class InMemoryLockManager:
    """A single-process reference :class:`LockManager` with real fencing tokens.

    Lease state lives in a dict guarded by an :class:`asyncio.Lock`; the fence
    counter is per-resource and monotonically increasing across the manager's
    lifetime, so even after release+reacquire the new holder's token strictly
    exceeds the old one (which is exactly what fencing requires).
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._held: dict[str, Lease] = {}
        self._fence: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, resource: str, *, owner: str | None = None, ttl_s: float = 30.0
    ) -> Lease | None:
        owner = owner or uuid.uuid4().hex
        now = self._clock.now()
        async with self._lock:
            current = self._held.get(resource)
            if current is not None and current.is_valid_at(now):
                return None  # someone else holds a live lease
            fence = self._fence.get(resource, 0) + 1
            self._fence[resource] = fence
            lease = Lease(
                resource=resource,
                owner=owner,
                fence=fence,
                expires_at=now + timedelta(seconds=ttl_s),
            )
            self._held[resource] = lease
            return lease

    async def renew(self, lease: Lease, *, ttl_s: float = 30.0) -> Lease | None:
        now = self._clock.now()
        async with self._lock:
            current = self._held.get(lease.resource)
            if current is None or current.owner != lease.owner or current.fence != lease.fence:
                return None
            refreshed = Lease(
                resource=lease.resource,
                owner=lease.owner,
                fence=lease.fence,
                expires_at=now + timedelta(seconds=ttl_s),
            )
            self._held[lease.resource] = refreshed
            return refreshed

    async def release(self, lease: Lease) -> bool:
        async with self._lock:
            current = self._held.get(lease.resource)
            if (
                current is not None
                and current.owner == lease.owner
                and current.fence == lease.fence
            ):
                del self._held[lease.resource]
                return True
            return False

    async def is_held(self, resource: str) -> bool:
        now = self._clock.now()
        async with self._lock:
            current = self._held.get(resource)
            return current is not None and current.is_valid_at(now)

    async def acquire_blocking(
        self,
        resource: str,
        *,
        owner: str | None = None,
        ttl_s: float = 30.0,
        wait_s: float = 30.0,
        poll_s: float = 0.5,
    ) -> Lease:
        """Retry :meth:`acquire` until it succeeds or ``wait_s`` of (virtual) time passes."""
        deadline = self._clock.now() + timedelta(seconds=wait_s)
        owner = owner or uuid.uuid4().hex
        while True:
            lease = await self.acquire(resource, owner=owner, ttl_s=ttl_s)
            if lease is not None:
                return lease
            if self._clock.now() >= deadline:
                raise LockAcquireTimeout(resource)
            await self._clock.sleep(poll_s)


# Acquire iff free, minting + returning a fresh fencing token.
# KEYS=[lock_key, fence_key]; ARGV=[owner, ttl_ms]
_LOCK_ACQUIRE_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return redis.call('INCR', KEYS[2])
else
    return -1
end
"""

# Renew iff still owned (keeps the same fencing token). KEYS=[lock_key]; ARGV=[owner, ttl_ms]
_LOCK_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
else
    return 0
end
"""

# Release iff still owned. KEYS=[lock_key]; ARGV=[owner]
_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class RedisLockManager:
    """A cross-process :class:`LockManager` over Redis with fencing tokens.

    The lock is a ``SET NX PX`` key holding the owner token; a companion
    per-resource ``INCR`` counter mints the monotonically increasing fencing
    token returned on each successful acquire. Renew/release are owner-scoped Lua
    compare-and-act scripts — the same minimal, proven surface as
    :class:`app.jobs.lease.LeaderLease`.
    """

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:saga:lock",
        clock: Clock | None = None,
    ) -> None:
        self._redis = getattr(redis, "raw", redis)
        self._ns = namespace
        self._clock = clock or SystemClock()

    def _lock_key(self, resource: str) -> str:
        return f"{self._ns}:{resource}"

    def _fence_key(self, resource: str) -> str:
        return f"{self._ns}:{resource}:fence"

    async def acquire(
        self, resource: str, *, owner: str | None = None, ttl_s: float = 30.0
    ) -> Lease | None:
        owner = owner or uuid.uuid4().hex
        ttl_ms = int(ttl_s * 1000)
        result = await self._redis.eval(
            _LOCK_ACQUIRE_LUA,
            2,
            self._lock_key(resource),
            self._fence_key(resource),
            owner,
            str(ttl_ms),
        )
        fence = int(result)
        if fence < 0:
            return None
        return Lease(
            resource=resource,
            owner=owner,
            fence=fence,
            expires_at=self._clock.now() + timedelta(seconds=ttl_s),
        )

    async def renew(self, lease: Lease, *, ttl_s: float = 30.0) -> Lease | None:
        ttl_ms = int(ttl_s * 1000)
        ok = await self._redis.eval(
            _LOCK_RENEW_LUA, 1, self._lock_key(lease.resource), lease.owner, str(ttl_ms)
        )
        if not int(ok):
            return None
        return Lease(
            resource=lease.resource,
            owner=lease.owner,
            fence=lease.fence,
            expires_at=self._clock.now() + timedelta(seconds=ttl_s),
        )

    async def release(self, lease: Lease) -> bool:
        deleted = await self._redis.eval(
            _LOCK_RELEASE_LUA, 1, self._lock_key(lease.resource), lease.owner
        )
        return bool(int(deleted))

    async def is_held(self, resource: str) -> bool:
        return (await self._redis.get(self._lock_key(resource))) is not None


class FencedResource:
    """Enforce monotonic fencing on writes to one logical resource.

    A protected resource embeds a :class:`FencedResource` and calls :meth:`guard`
    with the fencing token of every write. ``guard`` accepts the token iff it is
    ``>=`` the highest token previously accepted, updating the high-water mark;
    otherwise it raises :class:`StaleFenceError`. This is the piece that makes a
    fencing token *mean* something — without a resource that checks it, a fencing
    token is just a number. Accepting ``>=`` (not strictly ``>``) lets the same
    holder issue multiple writes under one lease while still rejecting any token
    from a superseded holder.
    """

    def __init__(self, resource: str) -> None:
        self._resource = resource
        self._highest = 0
        self._lock = asyncio.Lock()

    @property
    def highest_fence(self) -> int:
        """The highest fencing token accepted so far (0 == none yet)."""
        return self._highest

    async def guard(self, fence: int) -> None:
        """Accept ``fence`` iff monotonic; raise :class:`StaleFenceError` otherwise."""
        async with self._lock:
            if fence < self._highest:
                raise StaleFenceError(self._resource, fence, self._highest)
            self._highest = fence


__all__ = [
    "FencedResource",
    "InMemoryLockManager",
    "Lease",
    "LockAcquireTimeout",
    "LockManager",
    "RedisLockManager",
    "StaleFenceError",
]
