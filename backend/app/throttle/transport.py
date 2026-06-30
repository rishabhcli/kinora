"""The atomic execution seam: *compute-units* and the transports that run them.

Every distributed limiter in this package is, at heart, a **read-modify-write on
shared state that must be atomic across processes**. Redis gives us atomicity via
server-side Lua: a script sees a consistent snapshot and runs to completion with
no interleaving. So each algorithm is expressed as a :class:`ComputeUnit` — a
tiny program with two equivalent bodies:

* a **Lua source** string for production (``EVAL`` on the real driver), and
* a **Python ``apply``** that runs the *same logic* against an in-memory store.

The two must be behaviourally identical; the test suite is the contract. Keeping
both lets production use real redis atomicity while tests run the exact same
algorithm deterministically with a :class:`ManualClock` and an in-memory store —
no redis, no network, no sleeps.

A :class:`Transport` is "execute this unit atomically against these keys/args at
this server-time". Two implementations:

* :class:`InMemoryScriptTransport` — the emulator. Executes ``unit.apply`` under
  an ``asyncio.Lock`` so concurrent ``await``\\ s serialise exactly as redis
  would. Backed by :class:`InMemoryStore` (string + hash + ttl semantics, enough
  for every unit here).

* :class:`RedisScriptTransport` — wraps the app's :class:`~app.redis.RedisClient`
  and ``EVAL``\\ s ``unit.lua``. ``now`` is taken from the redis ``TIME`` command
  so the *server* clock is authoritative across the fleet (callers' clocks may be
  skewed). On any driver/connection error it raises
  :class:`~app.throttle.errors.StoreUnavailable` for the client to fail open/closed.

The unit ``apply`` receives ``now`` as an explicit float so the emulator is a pure
function of (state, args, time): the bedrock of deterministic tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.throttle.clock import (
    Clock,
    MonotonicClock,
    redis_time_to_seconds,
    seconds_to_redis_time,
)
from app.throttle.errors import StoreUnavailable

#: A unit's return value. Units here return a flat list of numbers (redis Lua
#: returns arrays of integers/bulk strings); the algorithm layer decodes it.
UnitResult = list[float]


class Store(Protocol):
    """The minimal redis-shaped key/value surface a :class:`ComputeUnit` may use.

    Deliberately tiny: string get/set with optional ttl, hash field get/set, key
    delete, and ttl introspection. Every algorithm in this package is expressible
    within this surface, which keeps the Lua and the emulator small and obviously
    equivalent. Values are floats on the way in/out; the emulator stores them
    verbatim and the Lua scripts use ``tonumber``.
    """

    def get(self, key: str) -> float | None: ...
    def set(self, key: str, value: float, *, ttl_s: float | None = None) -> None: ...
    def hget(self, key: str, field: str) -> float | None: ...
    def hset(
        self, key: str, mapping: Mapping[str, float], *, ttl_s: float | None = None
    ) -> None: ...
    def hgetall(self, key: str) -> dict[str, float]: ...
    def delete(self, key: str) -> None: ...
    def pexpire(self, key: str, ttl_s: float) -> None: ...
    # Sorted-set surface (used by the sliding-window-log and lease registries).
    def zadd(self, key: str, member: str, score: float, *, ttl_s: float | None = None) -> None: ...
    def zrem_range_by_score(self, key: str, min_score: float, max_score: float) -> int: ...
    def zcard(self, key: str) -> int: ...
    def zmin_score(self, key: str) -> float | None: ...
    def zrem(self, key: str, member: str) -> int: ...
    def zmembers(self, key: str) -> dict[str, float]: ...


@dataclass(frozen=True, slots=True)
class ComputeUnit:
    """An atomic step: a name, a Lua body, and an equivalent Python ``apply``.

    The Lua body and ``apply`` must compute the same ``UnitResult`` for the same
    ``(state, keys, args, now)``. Production runs ``lua`` under ``EVAL``; tests run
    ``apply`` under the emulator. ``key_count`` documents how many of the runtime
    ``keys`` redis should treat as keys (``KEYS`` vs ``ARGV`` split).
    """

    name: str
    lua: str
    key_count: int
    #: ``apply(store, keys, args, now) -> UnitResult``. Pure given the store state.
    apply: Any = field(repr=False)


class Transport(Protocol):
    """Run a :class:`ComputeUnit` atomically. ``now`` is server-authoritative."""

    async def run(
        self,
        unit: ComputeUnit,
        keys: list[str],
        args: list[float],
    ) -> UnitResult: ...

    async def server_time(self) -> float:
        """Current server time in seconds (authoritative across the fleet)."""
        ...


# --------------------------------------------------------------------------- #
# In-memory store + emulator transport (tests + fail-open local fallback)
# --------------------------------------------------------------------------- #


@dataclass
class _Entry:
    """A stored value with an optional absolute expiry (in store-clock seconds).

    ``kind`` disambiguates the two ``dict`` shapes: a hash (``"hash"``) vs a sorted
    set (``"zset"``, member->score). A scalar is ``"str"``. Mixing kinds on one key
    is a programming error and the accessors raise — same as redis' WRONGTYPE.
    """

    value: float | dict[str, float]
    expires_at: float | None
    kind: str = "str"


class InMemoryStore:
    """A deterministic redis-shaped store with lazy TTL expiry.

    "Lazy" mirrors redis: a key past its expiry is treated as absent on the next
    access (and dropped), rather than swept by a background timer — so behaviour
    depends only on the injected clock, never on real elapsed time. The store
    holds either a scalar (string semantics) or a ``dict`` (hash semantics) per
    key; mixing the two on one key is a programming error and raises.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._data: dict[str, _Entry] = {}
        self._clock = clock or MonotonicClock()

    # -- internal ttl handling ------------------------------------------- #

    def _live(self, key: str) -> _Entry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and self._clock.now() >= entry.expires_at:
            del self._data[key]
            return None
        return entry

    def _ttl_to_abs(self, ttl_s: float | None) -> float | None:
        return None if ttl_s is None else self._clock.now() + ttl_s

    # -- string ----------------------------------------------------------- #

    def get(self, key: str) -> float | None:
        entry = self._live(key)
        if entry is None:
            return None
        if entry.kind != "str":
            raise TypeError(f"{key!r} holds a {entry.kind}, not a scalar")
        assert not isinstance(entry.value, dict)
        return entry.value

    def set(self, key: str, value: float, *, ttl_s: float | None = None) -> None:
        self._data[key] = _Entry(float(value), self._ttl_to_abs(ttl_s), kind="str")

    # -- hash ------------------------------------------------------------- #

    def hget(self, key: str, field: str) -> float | None:
        entry = self._live(key)
        if entry is None:
            return None
        if entry.kind != "hash":
            raise TypeError(f"{key!r} holds a {entry.kind}, not a hash")
        assert isinstance(entry.value, dict)
        return entry.value.get(field)

    def hgetall(self, key: str) -> dict[str, float]:
        entry = self._live(key)
        if entry is None:
            return {}
        if entry.kind != "hash":
            raise TypeError(f"{key!r} holds a {entry.kind}, not a hash")
        assert isinstance(entry.value, dict)
        return dict(entry.value)

    def hset(
        self,
        key: str,
        mapping: Mapping[str, float],
        *,
        ttl_s: float | None = None,
    ) -> None:
        entry = self._live(key)
        if entry is None or entry.kind != "hash":
            entry = _Entry({}, self._ttl_to_abs(ttl_s), kind="hash")
            self._data[key] = entry
        assert isinstance(entry.value, dict)
        entry.value.update({k: float(v) for k, v in mapping.items()})
        if ttl_s is not None:
            entry.expires_at = self._ttl_to_abs(ttl_s)

    # -- sorted set ------------------------------------------------------- #

    def _zset(
        self, key: str, *, create: bool = False, ttl_s: float | None = None
    ) -> dict[str, float] | None:
        entry = self._live(key)
        if entry is None:
            if not create:
                return None
            entry = _Entry({}, self._ttl_to_abs(ttl_s), kind="zset")
            self._data[key] = entry
        if entry.kind != "zset":
            raise TypeError(f"{key!r} holds a {entry.kind}, not a zset")
        assert isinstance(entry.value, dict)
        return entry.value

    def zadd(self, key: str, member: str, score: float, *, ttl_s: float | None = None) -> None:
        zset = self._zset(key, create=True, ttl_s=ttl_s)
        assert zset is not None
        zset[member] = float(score)
        if ttl_s is not None:
            self._data[key].expires_at = self._ttl_to_abs(ttl_s)

    def zrem_range_by_score(self, key: str, min_score: float, max_score: float) -> int:
        zset = self._zset(key)
        if zset is None:
            return 0
        doomed = [m for m, s in zset.items() if min_score <= s <= max_score]
        for m in doomed:
            del zset[m]
        return len(doomed)

    def zcard(self, key: str) -> int:
        zset = self._zset(key)
        return 0 if zset is None else len(zset)

    def zmin_score(self, key: str) -> float | None:
        zset = self._zset(key)
        if not zset:
            return None
        return min(zset.values())

    def zrem(self, key: str, member: str) -> int:
        zset = self._zset(key)
        if zset is None or member not in zset:
            return 0
        del zset[member]
        return 1

    def zmembers(self, key: str) -> dict[str, float]:
        zset = self._zset(key)
        return {} if zset is None else dict(zset)

    # -- generic ---------------------------------------------------------- #

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def pexpire(self, key: str, ttl_s: float) -> None:
        entry = self._live(key)
        if entry is not None:
            entry.expires_at = self._ttl_to_abs(ttl_s)

    # -- test introspection ---------------------------------------------- #

    def keys(self) -> list[str]:
        """Live (non-expired) keys, for assertions."""
        return [k for k in list(self._data) if self._live(k) is not None]


class InMemoryScriptTransport:
    """Emulator transport: serialises ``unit.apply`` under one lock.

    The lock is what makes the emulator a faithful stand-in for redis atomicity —
    two coroutines that ``await run`` interleave at the ``await`` boundary, never
    *inside* a unit, exactly as two clients sharing one redis would see scripts run
    one-at-a-time. The store's clock is the same injected clock, so ``server_time``
    is the test's :class:`ManualClock`.
    """

    def __init__(self, store: InMemoryStore | None = None, *, clock: Clock | None = None) -> None:
        self._clock = clock or MonotonicClock()
        self.store = store or InMemoryStore(self._clock)
        self._lock = asyncio.Lock()

    async def run(
        self,
        unit: ComputeUnit,
        keys: list[str],
        args: list[float],
    ) -> UnitResult:
        async with self._lock:
            now = self._clock.now()
            result = unit.apply(self.store, keys, args, now)
            return [float(x) for x in result]

    async def server_time(self) -> float:
        return self._clock.now()


# --------------------------------------------------------------------------- #
# Redis transport (production)
# --------------------------------------------------------------------------- #


class RedisScriptTransport:
    """Production transport: ``EVAL``\\ s ``unit.lua`` on the shared redis.

    ``now`` is sourced from redis ``TIME`` and *prepended* to ``args`` so the
    server clock is the single source of truth across the fleet — a caller with a
    skewed clock cannot widen or shrink its own window. Any driver/connection
    failure surfaces as :class:`StoreUnavailable`; the client decides fail
    open/closed. We pass the raw driver (``redis_client.raw``) because we need
    ``EVAL`` and ``TIME``, which the typed wrapper does not expose.
    """

    def __init__(self, redis_client: Any) -> None:
        # Accept either app.redis.RedisClient (use .raw) or a raw driver.
        self._raw = getattr(redis_client, "raw", redis_client)

    async def server_time(self) -> float:
        try:
            redis_time = await self._raw.time()
        except Exception as exc:  # noqa: BLE001 - normalise all driver faults
            raise StoreUnavailable("redis TIME failed", cause=exc) from exc
        return redis_time_to_seconds([int(redis_time[0]), int(redis_time[1])])

    async def run(
        self,
        unit: ComputeUnit,
        keys: list[str],
        args: list[float],
    ) -> UnitResult:
        # Prepend authoritative server time as args[0] so the Lua body reads a
        # consistent ``now`` (passed as a [secs, micros] pair the script rejoins).
        try:
            redis_time = await self._raw.time()
            now_s = redis_time_to_seconds([int(redis_time[0]), int(redis_time[1])])
            now_pair = seconds_to_redis_time(now_s)
            argv: list[Any] = [now_pair[0], now_pair[1], *(repr(a) for a in args)]
            raw = await self._raw.eval(unit.lua, len(keys), *keys, *argv)
        except StoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise all driver faults
            raise StoreUnavailable(f"unit {unit.name!r} EVAL failed", cause=exc) from exc
        if raw is None:
            return []
        return [float(x) for x in raw]


__all__ = [
    "ComputeUnit",
    "InMemoryScriptTransport",
    "InMemoryStore",
    "RedisScriptTransport",
    "Store",
    "Transport",
    "UnitResult",
]
