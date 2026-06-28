"""Distributed leader election for periodic scheduling.

Only one node in a fleet should *evaluate triggers* (decide what is due and
enqueue runs) — otherwise N nodes each fire the same cron and, while idempotency
collapses the duplicate runs, the scheduling work is wasted and racy. This module
provides a Redis-backed **leader lease**: a single key held with ``SET NX PX``,
an owner token so only the holder can renew/release it, and a monotonically
increasing **fencing token** so a stalled-then-resumed old leader can be detected
and ignored by anything that cares about ordering.

:class:`LeaderLease` is the low-level primitive (acquire / renew / release).
:class:`LeaderElector` wraps it in a background renewal loop and exposes
``is_leader`` so the scheduler can gate its work; if renewal fails (the key
expired, or someone else took it) the elector drops leadership cleanly within one
renewal interval. Both take an injected :class:`~app.jobs.clock.Clock` so the
renewal cadence is virtual-clock friendly in tests.

The Redis surface used is intentionally tiny (``SET NX PX``, a Lua
compare-and-renew, a Lua compare-and-delete) and mirrors the pattern already
proven in :class:`app.redis.client.DistributedLock`, so it composes with the
existing client without touching it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from app.jobs.clock import Clock, SystemClock

# Renew the lease iff we still own it; bump the fencing counter and extend TTL.
# KEYS = [lease_key, fence_key]; ARGV = [owner_token, ttl_ms]
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return redis.call('GET', KEYS[2])
else
    return -1
end
"""

# Acquire iff free: SET NX PX, and on success bump+return the fencing counter.
# KEYS = [lease_key, fence_key]; ARGV = [owner_token, ttl_ms]
_ACQUIRE_LUA = """
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2])
if ok then
    return redis.call('INCR', KEYS[2])
else
    return -1
end
"""

# Release iff we still own it (owner-only delete).
# KEYS = [lease_key]; ARGV = [owner_token]
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LeaderLease:
    """A single, renewable, owner-scoped Redis lease with a fencing token."""

    def __init__(
        self,
        redis: Any,
        *,
        name: str = "kinora:jobs:leader",
        ttl_ms: int = 15_000,
        owner_token: str | None = None,
    ) -> None:
        self._redis = getattr(redis, "raw", redis)
        self._key = name
        self._fence_key = f"{name}:fence"
        self._ttl_ms = ttl_ms
        self._owner = owner_token or uuid.uuid4().hex
        self._fence: int | None = None
        self._held = False

    @property
    def owner_token(self) -> str:
        """This contender's unique owner token."""
        return self._owner

    @property
    def fence(self) -> int | None:
        """The fencing token from the last successful acquire/renew (``None`` if not held)."""
        return self._fence

    @property
    def held(self) -> bool:
        """Whether this instance currently believes it holds the lease."""
        return self._held

    async def acquire(self) -> bool:
        """Try to acquire the lease (non-blocking). Sets the fencing token on success."""
        result = await self._redis.eval(
            _ACQUIRE_LUA, 2, self._key, self._fence_key, self._owner, str(self._ttl_ms)
        )
        fence = int(result)
        if fence < 0:
            self._held = False
            return False
        self._fence = fence
        self._held = True
        return True

    async def renew(self) -> bool:
        """Extend the lease iff still owned. Returns ``False`` if leadership was lost."""
        result = await self._redis.eval(
            _RENEW_LUA, 2, self._key, self._fence_key, self._owner, str(self._ttl_ms)
        )
        fence = int(result)
        if fence < 0:
            self._held = False
            return False
        self._fence = fence
        self._held = True
        return True

    async def release(self) -> bool:
        """Release the lease iff we still own it (owner-only delete)."""
        deleted = await self._redis.eval(_RELEASE_LUA, 1, self._key, self._owner)
        self._held = False
        return bool(deleted)


class LeaderElector:
    """Maintain leadership in the background and expose :attr:`is_leader`.

    Run :meth:`start` to spawn the renewal loop and :meth:`stop` to tear it down
    (releasing the lease if held). The loop tries to acquire when not leader and
    renews when leader, every ``renew_interval_s`` (which must be comfortably less
    than the lease TTL so a renew lands before expiry). The scheduler reads
    :attr:`is_leader` synchronously to gate trigger evaluation.
    """

    def __init__(
        self,
        lease: LeaderLease,
        *,
        clock: Clock | None = None,
        renew_interval_s: float = 5.0,
    ) -> None:
        self._lease = lease
        self._clock = clock or SystemClock()
        self._renew_interval_s = renew_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_leader(self) -> bool:
        """Whether this node currently holds leadership."""
        return self._lease.held

    @property
    def fence(self) -> int | None:
        """The current fencing token (``None`` when not leader)."""
        return self._lease.fence if self._lease.held else None

    async def try_acquire(self) -> bool:
        """One-shot acquire attempt (used by tests + the first loop iteration)."""
        return await self._lease.acquire()

    async def tick(self) -> bool:
        """One election step: renew if leader, else try to acquire. Returns leadership."""
        leader = await self._tick_inner()
        from app.jobs import metrics

        metrics.set_leader(leader)
        return leader

    async def _tick_inner(self) -> bool:
        if self._lease.held:
            if not await self._lease.renew():
                # Lost it — try to re-acquire immediately so a brief blip self-heals.
                return await self._lease.acquire()
            return True
        return await self._lease.acquire()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
            await self._clock.sleep(self._renew_interval_s)

    def start(self) -> None:
        """Spawn the background renewal loop (idempotent)."""
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and release the lease if held."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self._lease.release()


__all__ = ["LeaderElector", "LeaderLease"]
