"""Distributed **concurrency leases**: at most ``N`` in-flight across the fleet.

A rate limiter bounds requests *per unit time*; a concurrency lease bounds
*simultaneous* requests. Kinora needs both — e.g. "no more than 4 video renders
running against Wan at once, fleet-wide", independent of how fast they start. A
single-process semaphore can't do this because the holders are different render
workers; the cap must live in shared state.

**Design.** One redis sorted set per pool, member = a unique holder id, score =
the lease's *expiry* (server time + TTL). Acquire is an atomic unit:

1. evict expired holders (``ZREMRANGEBYSCORE -inf now``) — this is the crash
   safety: a worker that dies without releasing has its lease auto-reclaimed
   once its TTL passes, so a crash can't permanently consume a slot;
2. if the live count ``< capacity``, add this holder scored at ``now + ttl`` and
   admit; else deny with a retry hint (the soonest expiry, i.e. when a slot is
   guaranteed free even if nobody releases voluntarily).

A held lease must be **renewed** (heartbeat) before its TTL or it is considered
crashed and reclaimed — so set the TTL comfortably above the heartbeat interval.
**Release** removes the member immediately (the fast path; expiry is only the
backstop). The :class:`Lease` context manager renews/releases for you.

Fairness note: this is a *counting* lease (any of N slots), not a FIFO queue —
callers compete on retry, which is the right trade-off for a fan-out of
interchangeable workers. A strict fair queue would need a separate ordered
waiting list; out of scope here, where the slots are fungible.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType

from app.throttle.errors import LeaseUnavailable
from app.throttle.transport import ComputeUnit, Store, Transport, UnitResult

#: Width of the monotonic counter half of a holder id (2**32 ids per prefix).
_COUNTER_BITS = 32


def _default_id_factory() -> Callable[[], int]:
    """A holder-id source: ``(21-bit random prefix << 32) | 32-bit counter``.

    Total 53 bits so it round-trips through float64 exactly. Unique within a
    process (the counter), and the random prefix makes a cross-process collision
    vanishingly unlikely. Returns a *fresh* closure per pool so two pools in one
    process get independent counters (the prefix still separates them).
    """
    prefix = secrets.randbits(21) << _COUNTER_BITS
    counter = itertools.count(1)
    return lambda: prefix | next(counter)

_ACQUIRE_LUA = """
-- KEYS[1] = pool zset; ARGV[1,2] = server time; ARGV[3] = capacity;
-- ARGV[4] = ttl; ARGV[5] = holder id
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local capacity = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local holder = ARGV[5]

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. now)
local count = redis.call('ZCARD', KEYS[1])
local acquired = 0
local retry_after = 0.0
if count < capacity then
  redis.call('ZADD', KEYS[1], now + ttl, holder)
  acquired = 1
  count = count + 1
else
  local soonest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  if soonest[2] ~= nil then
    retry_after = tonumber(soonest[2]) - now
    if retry_after < 0 then retry_after = 0 end
  end
end
redis.call('PEXPIRE', KEYS[1], math.ceil(ttl * 1000))
return {acquired, count, math.floor(retry_after * 1000000)}
"""


def _acquire_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    capacity = int(args[0])
    ttl = args[1]
    holder = str(int(args[2]))  # holder ids are passed as a stable int seed

    # Evict expired holders (expiry strictly before now).
    store.zrem_range_by_score(key, float("-inf"), now - 1e-12)
    count = store.zcard(key)
    acquired = 0.0
    retry_after = 0.0
    if count < capacity:
        store.zadd(key, holder, now + ttl, ttl_s=ttl)
        acquired = 1.0
        count += 1
    else:
        soonest = store.zmin_score(key)
        if soonest is not None:
            retry_after = max(0.0, soonest - now)
        store.pexpire(key, ttl)
    return [acquired, float(count), retry_after * 1_000_000]


LEASE_ACQUIRE_UNIT = ComputeUnit(
    name="lease_acquire",
    lua=_ACQUIRE_LUA,
    key_count=1,
    apply=_acquire_apply,
)

_RENEW_LUA = """
-- KEYS[1] = pool zset; ARGV[1,2] = server time; ARGV[3] = ttl; ARGV[4] = holder
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local ttl = tonumber(ARGV[3])
local holder = ARGV[4]
local score = redis.call('ZSCORE', KEYS[1], holder)
if score == nil then return {0} end
-- Only renew if not already expired (a reclaimed lease must not resurrect).
if tonumber(score) < now then
  redis.call('ZREM', KEYS[1], holder)
  return {0}
end
redis.call('ZADD', KEYS[1], now + ttl, holder)
redis.call('PEXPIRE', KEYS[1], math.ceil(ttl * 1000))
return {1}
"""


def _renew_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    ttl = args[0]
    holder = str(int(args[1]))
    members = store.zmembers(key)
    score = members.get(holder)
    if score is None:
        return [0.0]
    if score < now:
        store.zrem(key, holder)
        return [0.0]
    store.zadd(key, holder, now + ttl, ttl_s=ttl)
    return [1.0]


LEASE_RENEW_UNIT = ComputeUnit(
    name="lease_renew",
    lua=_RENEW_LUA,
    key_count=1,
    apply=_renew_apply,
)

_RELEASE_LUA = """
-- KEYS[1] = pool zset; ARGV[1] = holder
return {redis.call('ZREM', KEYS[1], ARGV[1])}
"""


def _release_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    return [float(store.zrem(keys[0], str(int(args[0]))))]


LEASE_RELEASE_UNIT = ComputeUnit(
    name="lease_release",
    lua=_RELEASE_LUA,
    key_count=1,
    apply=_release_apply,
)


@dataclass(frozen=True, slots=True)
class LeaseConfig:
    """``capacity`` simultaneous holders fleet-wide; each lease lives ``ttl_s``.

    ``ttl_s`` is the crash backstop: a holder that dies has its slot reclaimed
    ``ttl_s`` after its last renew. Keep ``ttl_s`` > the heartbeat interval (a
    common choice is ``ttl_s = 3 * heartbeat``) so a momentarily slow renew
    doesn't lose the lease.
    """

    capacity: int
    ttl_s: float

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be >= 1")
        if self.ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")


class Lease:
    """A held concurrency slot. Async context manager: enter acquires (or raises
    :class:`~app.throttle.errors.LeaseUnavailable`), exit releases.

    Renew with :meth:`renew` from a heartbeat; :attr:`held` reports whether the
    lease is still ours (a renew that finds the lease reclaimed flips it false).
    """

    def __init__(self, pool: ConcurrencyLeasePool, holder_id: int) -> None:
        self._pool = pool
        self._holder_id = holder_id
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def holder_id(self) -> int:
        return self._holder_id

    async def acquire(self) -> bool:
        acquired, _count, _retry = await self._pool._raw_acquire(self._holder_id)
        self._held = acquired
        return acquired

    async def renew(self) -> bool:
        ok = await self._pool._raw_renew(self._holder_id)
        self._held = ok
        return ok

    async def release(self) -> None:
        if self._held:
            await self._pool._raw_release(self._holder_id)
            self._held = False

    async def __aenter__(self) -> Lease:
        acquired, count, retry_after = await self._pool._raw_acquire(self._holder_id)
        if not acquired:
            raise LeaseUnavailable(
                retry_after,
                scope=self._pool.scope,
                in_flight=count,
                capacity=self._pool.capacity,
            )
        self._held = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


class ConcurrencyLeasePool:
    """A fleet-wide cap of ``capacity`` in-flight holders for one scope."""

    def __init__(
        self,
        transport: Transport,
        scope: str,
        config: LeaseConfig,
        *,
        key_prefix: str = "throttle:lease",
        id_factory: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._scope = scope
        self._config = config
        self._key = f"{key_prefix}:{scope}"
        # Holder ids must be unique per *attempt* fleet-wide AND must round-trip
        # exactly through a float64 (the transport carries args as floats), so the
        # usable space is the exact-integer range below 2**53. A uuid mod 1e15
        # collides far too readily (overwriting a ZADD member silently uncaps the
        # pool — a real bug we hit). Instead: a 21-bit per-process random prefix +
        # a 32-bit monotonic counter = 53 bits, collision-free within a process and
        # astronomically unlikely to collide across processes for a pool's life.
        self._id_factory = id_factory or _default_id_factory()

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def capacity(self) -> int:
        return self._config.capacity

    def lease(self, holder_id: int | None = None) -> Lease:
        """A new :class:`Lease` handle (not yet acquired)."""
        return Lease(self, holder_id if holder_id is not None else self._id_factory())

    async def try_acquire(self, holder_id: int | None = None) -> Lease | None:
        """Acquire without raising; ``None`` if the pool is full."""
        lease = self.lease(holder_id)
        if await lease.acquire():
            return lease
        return None

    async def in_flight(self) -> int:
        """Best-effort live holder count (evicts expired as a side effect of a
        zero-capacity probe would mutate; instead we just acquire+immediately
        release a phantom to read the post-eviction count).

        Implemented as a renew of a non-existent holder, which performs the same
        eviction sweep server-side and returns the count via a dedicated unit.
        """
        out = await self._transport.run(
            LEASE_ACQUIRE_UNIT,
            [self._key],
            [0.0, self._config.ttl_s, float(self._id_factory())],
        )
        # capacity=0 means we never add; count reflects post-eviction live holders.
        return int(out[1])

    # -- raw atomic ops (used by Lease) ---------------------------------- #

    async def _raw_acquire(self, holder_id: int) -> tuple[bool, int, float]:
        out = await self._transport.run(
            LEASE_ACQUIRE_UNIT,
            [self._key],
            [float(self._config.capacity), self._config.ttl_s, float(holder_id)],
        )
        return (out[0] >= 0.5, int(out[1]), out[2] / 1_000_000)

    async def _raw_renew(self, holder_id: int) -> bool:
        out = await self._transport.run(
            LEASE_RENEW_UNIT,
            [self._key],
            [self._config.ttl_s, float(holder_id)],
        )
        return out[0] >= 0.5

    async def _raw_release(self, holder_id: int) -> bool:
        out = await self._transport.run(
            LEASE_RELEASE_UNIT,
            [self._key],
            [float(holder_id)],
        )
        return out[0] >= 0.5


#: An async sleep seam (so callers/tests can advance a fake clock).
SleepFn = Callable[[float], Awaitable[None]]


__all__ = [
    "LEASE_ACQUIRE_UNIT",
    "LEASE_RELEASE_UNIT",
    "LEASE_RENEW_UNIT",
    "ConcurrencyLeasePool",
    "Lease",
    "LeaseConfig",
    "SleepFn",
]
