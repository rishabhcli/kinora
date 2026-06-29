"""A fault-injecting Redis proxy over the real in-memory double (kinora.md §12.1 —
the render queue lives on the managed broker; the simulator drives the *real*
:class:`~app.queue.redis_queue.RedisRenderQueue` against this).

The point of difference from a hand-rolled queue model: this seam wraps the
project's own :class:`~app.queue.fakeredis.FakeAsyncRedis` and exposes the exact
duck-typed surface ``RedisRenderQueue`` calls (``eval``, ``zadd``, ``hset``, …).
So the simulation exercises the **production queue code** — its Lua-equivalent
enqueue/claim atomicity, its lease bookkeeping, its idempotency index, its DLQ —
not a re-implementation that could drift. We only interpose a fault layer.

Three faults live here, each a real broker failure mode:

* ``REDIS_ERROR`` — a command raises a connection error. The queue/worker must
  treat it as transient and recover (its callers are wrapped in retry).
* ``REDIS_SLOW`` — extra latency on a command. Because the real queue is ``async``
  and the runtime pumps it against the virtual clock, "slow redis" is reported via
  an ``on_latency`` hook the runtime folds into clock advancement — no wall sleep.
* ``REDIS_FLUSH`` — a failover/flush wipes *volatile* keys. We model the realistic
  case: ephemeral session/keyframe state can vanish, but we never silently corrupt
  the durable job records the DLQ and idempotency index depend on (a flush that
  ate the DLQ would be a different, out-of-scope disaster). This stresses the
  scheduler's ability to re-seed a session whose redis-backed state evaporated.

The proxy holds NO ``.raw`` attribute deliberately: ``RedisRenderQueue`` does
``getattr(redis, "raw", redis)`` and we *want* it to use the proxy itself so every
command flows through the fault layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.queue.fakeredis import FakeAsyncRedis
from app.verification.simulation.buggify import Buggify
from app.verification.simulation.faults import FaultKind


class SimRedisError(ConnectionError):
    """A simulated transient redis connection failure (callers should retry)."""


#: Commands that mutate or read queue state and are subject to fault injection.
#: (Lifecycle/no-op commands like ``close`` are passed straight through.)
_FAULTED_COMMANDS = frozenset(
    {
        "eval", "get", "set", "incr", "expire", "delete",
        "hset", "hget", "hgetall",
        "sadd", "srem", "smembers", "scard",
        "zadd", "zrem", "zcard", "zscore", "zrangebyscore",
        "lpush", "rpush", "lrange", "llen", "lrem",
        "scan", "publish",
    }
)


class FaultingRedis:
    """An async proxy that injects faults around a real :class:`FakeAsyncRedis`.

    Every faultable command rolls :class:`Buggify`: an error roll raises before
    the command runs (so no state changes — the cleanest transient), a latency
    roll reports virtual delay through ``on_latency``. Unmodelled / lifecycle
    attributes delegate straight through, so the proxy stays a faithful stand-in
    for the production redis client.
    """

    __slots__ = ("_inner", "_buggify", "_on_latency", "command_count", "error_count")

    def __init__(
        self,
        inner: FakeAsyncRedis,
        buggify: Buggify,
        *,
        on_latency: Callable[[int], None] | None = None,
    ) -> None:
        self._inner = inner
        self._buggify = buggify
        #: Folds simulated command latency back into the virtual clock.
        self._on_latency = on_latency or (lambda _ms: None)
        self.command_count = 0
        self.error_count = 0

    @property
    def inner(self) -> FakeAsyncRedis:
        """The wrapped real double (for tests / direct flush)."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        # Note: __slots__ means our own attrs are found before __getattr__; this
        # only fires for commands and lifecycle methods on the inner client.
        target = getattr(self._inner, name)
        if name not in _FAULTED_COMMANDS or not callable(target):
            return target

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            self.command_count += 1
            if self._buggify.should(FaultKind.REDIS_ERROR, f"redis.{name}"):
                self.error_count += 1
                raise SimRedisError(f"transient redis failure on {name!r}")
            delay = self._buggify.duration(FaultKind.REDIS_SLOW, f"redis.{name}")
            if delay:
                self._on_latency(delay)
            return await target(*args, **kwargs)

        return _wrapped

    def maybe_flush(self, volatile_prefixes: tuple[str, ...]) -> int:
        """Roll for a failover flush of volatile keys; return how many vanished.

        Only keys under ``volatile_prefixes`` (session/keyframe ephemera) are
        eligible — durable queue records (jobs, DLQ, idempotency) are preserved so
        the test stays focused on session re-seed recovery, not data-loss chaos.
        Called by the runtime at discrete points, not per-command.
        """
        if not self._buggify.should(FaultKind.REDIS_FLUSH, "redis.flush"):
            return 0
        inner = self._inner
        # FakeAsyncRedis spreads keys across typed maps; gather them all.
        all_keys = (
            set(inner._strings)
            | set(inner._hashes)
            | set(inner._sets)
            | set(inner._zsets)
            | set(inner._lists)
        )
        victims = [k for k in all_keys if any(k.startswith(p) for p in volatile_prefixes)]
        for k in victims:
            inner._drop(k)
        return len(victims)


def install_virtual_clock(inner: FakeAsyncRedis, clock_s: Callable[[], float]) -> None:
    """Point a :class:`FakeAsyncRedis`'s TTL clock at the simulation's clock.

    ``FakeAsyncRedis`` honours key TTLs against ``time.monotonic`` by default,
    which would make expiry depend on wall-clock and break reproducibility. The
    queue uses long TTLs (success/token in the thousands of seconds), so under the
    virtual clock a normal run never expires them — exactly the production intent —
    while staying a pure function of the seed.
    """
    inner._clock = clock_s


__all__ = [
    "FaultingRedis",
    "SimRedisError",
    "install_virtual_clock",
]
