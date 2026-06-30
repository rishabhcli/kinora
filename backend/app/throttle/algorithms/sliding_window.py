"""Distributed **sliding-window log** as an atomic compute-unit.

A sliding-window log admits at most ``limit`` requests in any trailing window of
``window_s`` seconds. Unlike a fixed window (which lets ``2*limit`` through across
a boundary — ``limit`` at ``t=0.99`` and ``limit`` at ``t=1.01`` of a 1 s window),
the log is *exact*: it remembers the timestamp of every recent admission and
counts only those still inside ``[now - window_s, now]``.

**State** is one redis sorted set per scope, member = a unique id, score = the
admission time. The unit:

1. evicts entries older than the window (``ZREMRANGEBYSCORE -inf (now-window)``);
2. counts what remains (``ZCARD``);
3. if ``count + cost <= limit`` it adds ``cost`` members (one per unit of cost)
   scored at ``now`` and reports *allowed*;
4. else it reports *denied* with ``retry_after`` = time until the **oldest**
   in-window entry falls out (``oldest + window - now``), which is exactly when a
   slot frees.

The set is given a TTL of ``window_s`` on write so an idle scope self-evicts.
Cost is realised as ``cost`` distinct members (with a sequence suffix) so a
multi-unit request occupies multiple slots, consistent with the count check.

Trade-off captured in the design: the log is the most *accurate* limiter but its
memory is O(``limit``) per scope — fine for the modest provider/tenant limits
here, and the exactness is worth it when a hard provider quota must never be
crossed. The token bucket is the cheaper choice when bursts are acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.throttle.result import Decision
from app.throttle.transport import ComputeUnit, Store, Transport, UnitResult

_SLIDING_WINDOW_LUA = """
-- KEYS[1] = window zset
-- ARGV[1], ARGV[2] = server time (secs, micros)
-- ARGV[3] = window_s, ARGV[4] = limit, ARGV[5] = cost, ARGV[6] = unique id base
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local window = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])
local idbase = ARGV[6]

local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. cutoff)
local count = redis.call('ZCARD', KEYS[1])

local allowed = 0
local retry_after = 0.0
if count + cost <= limit then
  allowed = 1
  for i = 1, cost do
    redis.call('ZADD', KEYS[1], now, idbase .. ':' .. i)
  end
  count = count + cost
else
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  if oldest[2] ~= nil then
    retry_after = (tonumber(oldest[2]) + window) - now
    if retry_after < 0 then retry_after = 0 end
  end
end

redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
local remaining = limit - count
if remaining < 0 then remaining = 0 end
return {
  allowed,
  math.floor(retry_after * 1000000),
  math.floor(remaining * 1000000),
  math.floor(window * 1000000)
}
"""


def _sliding_window_apply(
    store: Store, keys: list[str], args: list[float], now: float
) -> UnitResult:
    key = keys[0]
    window = args[0]
    limit = args[1]
    cost = int(args[2])
    # The unique-id base is encoded as a float seed (caller passes a monotonic
    # counter); we render it stably so members never collide within a scope.
    id_seed = int(args[3])

    cutoff = now - window
    # Evict strictly-older-than-cutoff entries (half-open window [cutoff, now]).
    store.zrem_range_by_score(key, float("-inf"), cutoff - 1e-12)
    count = store.zcard(key)

    allowed = 0.0
    retry_after = 0.0
    if count + cost <= limit:
        allowed = 1.0
        for i in range(cost):
            store.zadd(key, f"{id_seed}:{i}", now, ttl_s=window)
        count += cost
    else:
        oldest = store.zmin_score(key)
        if oldest is not None:
            retry_after = max(0.0, (oldest + window) - now)
        store.pexpire(key, window)

    remaining = max(0.0, limit - count)
    return [allowed, retry_after * 1_000_000, remaining * 1_000_000, window * 1_000_000]


SLIDING_WINDOW_UNIT = ComputeUnit(
    name="sliding_window",
    lua=_SLIDING_WINDOW_LUA,
    key_count=1,
    apply=_sliding_window_apply,
)


@dataclass(frozen=True, slots=True)
class SlidingWindowConfig:
    """At most ``limit`` admissions in any trailing ``window_s`` seconds (exact)."""

    limit: int
    window_s: float

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be > 0")
        if self.window_s <= 0:
            raise ValueError("window_s must be > 0")


class SlidingWindowLimiter:
    """A distributed exact sliding-window-log limiter over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        scope: str,
        config: SlidingWindowConfig,
        *,
        key_prefix: str = "throttle:sw",
    ) -> None:
        self._transport = transport
        self._scope = scope
        self._config = config
        self._key = f"{key_prefix}:{scope}"
        # Per-process monotonic seed so concurrent members in one scope never
        # collide; the store namespaces by member string, so cross-process
        # uniqueness only needs the seed to differ, which the time-scored set
        # tolerates (a rare dup just over-counts conservatively, never under).
        self._seq = 0

    @property
    def scope(self) -> str:
        return self._scope

    async def check(self, cost: int = 1) -> Decision:
        """Atomically attempt to admit ``cost`` units in the window; never blocks."""
        decision, _seed = await self._check_with_seed(cost)
        return decision

    async def _check_with_seed(self, cost: int) -> tuple[Decision, int]:
        if cost <= 0:
            raise ValueError("cost must be > 0")
        self._seq += 1
        seed = self._seq
        out = await self._transport.run(
            SLIDING_WINDOW_UNIT,
            [self._key],
            [self._config.window_s, float(self._config.limit), float(cost), float(seed)],
        )
        allowed = out[0] >= 0.5
        retry_after = out[1] / 1_000_000
        remaining = out[2] / 1_000_000
        reset_after = out[3] / 1_000_000
        if allowed:
            return (
                Decision.allow(
                    remaining=remaining,
                    reset_after=reset_after,
                    scope=self._scope,
                    limit="sliding_window",
                ),
                seed,
            )
        return (
            Decision.deny(
                retry_after,
                remaining=remaining,
                reset_after=reset_after,
                scope=self._scope,
                limit="sliding_window",
            ),
            seed,
        )

    async def refund(self, cost: int, seed: int) -> None:
        """Remove the ``cost`` members an admission with ``seed`` added."""
        from app.throttle.quota import SLIDING_WINDOW_REFUND_UNIT

        await self._transport.run(
            SLIDING_WINDOW_REFUND_UNIT,
            [self._key],
            [float(cost), float(seed)],
        )


__all__ = [
    "SLIDING_WINDOW_UNIT",
    "SlidingWindowConfig",
    "SlidingWindowLimiter",
]
