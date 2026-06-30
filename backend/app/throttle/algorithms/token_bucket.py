"""Distributed **token bucket** as an atomic compute-unit.

A bucket of ``capacity`` tokens refills continuously at ``rate`` tokens/second.
A request of ``cost`` tokens is admitted iff at least ``cost`` are present, and
consumes them; otherwise it is denied with the exact wait until the deficit
refills. This is the classic "allow bursts up to ``capacity``, sustain ``rate``"
limiter — ideal for provider quotas that publish a steady RPS with some burst.

**State** lives in one redis hash per scope: ``tokens`` (current count) and ``ts``
(the timestamp tokens were last computed). The unit:

1. lazily refills — ``tokens = min(capacity, tokens + (now - ts) * rate)`` — so
   no background timer is needed (refill is computed at read time);
2. if ``tokens >= cost`` it subtracts and reports *allowed*;
3. else it computes ``retry_after = (cost - tokens) / rate`` and reports *denied*
   **without** consuming (a denied request must not drain the bucket).

The hash is given a TTL of ``capacity / rate`` (the full-refill time) on every
write so idle scopes self-evict — an unused per-book bucket doesn't leak a key
forever, and a key that reappears starts full, which is correct (a long-idle
scope *should* have a full bucket).

The Lua body and the Python ``apply`` implement this identically; the test suite
asserts both refill math and retry-after accuracy against a manual clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.throttle.result import Decision
from app.throttle.transport import ComputeUnit, Store, Transport, UnitResult

# args: now_lo? handled by transport; here args = [rate, capacity, cost]
# We reconstruct now from KEYS-adjacent ARGV in Lua; the emulator gets `now` direct.
_TOKEN_BUCKET_LUA = """
-- KEYS[1] = bucket hash
-- ARGV[1], ARGV[2] = server time (secs, micros) injected by the transport
-- ARGV[3] = rate (tokens/s), ARGV[4] = capacity, ARGV[5] = cost
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local rate = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0.0
if tokens + 1e-9 >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
local ttl_ms = math.ceil((capacity / rate) * 1000)
redis.call('PEXPIRE', KEYS[1], ttl_ms)

local reset_after = (capacity - tokens) / rate
-- return scaled-by-1e6 ints so floats survive redis' integer return coercion
return {
  allowed,
  math.floor(retry_after * 1000000),
  math.floor(tokens * 1000000),
  math.floor(reset_after * 1000000)
}
"""

#: Token tolerance for "enough tokens" — absorbs float dust so a refill landing a
#: hair under ``cost`` is treated as available (mirrors the Round-1 bucket).
_EPSILON = 1e-9


def _token_bucket_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    rate, capacity, cost = args[0], args[1], args[2]

    state = store.hgetall(key)
    tokens = state.get("tokens")
    ts = state.get("ts")
    if tokens is None or ts is None:
        tokens = capacity
        ts = now

    elapsed = max(0.0, now - ts)
    tokens = min(capacity, tokens + elapsed * rate)

    allowed = 0.0
    retry_after = 0.0
    if tokens + _EPSILON >= cost:
        tokens -= cost
        allowed = 1.0
    else:
        retry_after = (cost - tokens) / rate

    ttl_s = capacity / rate
    store.hset(key, {"tokens": tokens, "ts": now}, ttl_s=ttl_s)
    reset_after = (capacity - tokens) / rate
    return [allowed, retry_after * 1_000_000, tokens * 1_000_000, reset_after * 1_000_000]


TOKEN_BUCKET_UNIT = ComputeUnit(
    name="token_bucket",
    lua=_TOKEN_BUCKET_LUA,
    key_count=1,
    apply=_token_bucket_apply,
)


@dataclass(frozen=True, slots=True)
class TokenBucketConfig:
    """Sustained ``rate`` tokens/s with bursts up to ``capacity`` tokens."""

    rate: float
    capacity: float

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be > 0")
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")


class TokenBucketLimiter:
    """A distributed token-bucket limiter over a :class:`Transport`.

    Stateless beyond config + the scope key — all mutable state is in the store,
    so any number of processes share one limiter for a scope by using the same
    key. :meth:`check` is one atomic round-trip.
    """

    def __init__(
        self,
        transport: Transport,
        scope: str,
        config: TokenBucketConfig,
        *,
        key_prefix: str = "throttle:tb",
    ) -> None:
        self._transport = transport
        self._scope = scope
        self._config = config
        self._key = f"{key_prefix}:{scope}"

    @property
    def scope(self) -> str:
        return self._scope

    async def check(self, cost: float = 1.0) -> Decision:
        """Atomically attempt to consume ``cost`` tokens; never blocks."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        out = await self._transport.run(
            TOKEN_BUCKET_UNIT,
            [self._key],
            [self._config.rate, self._config.capacity, cost],
        )
        allowed = out[0] >= 0.5
        retry_after = out[1] / 1_000_000
        remaining = out[2] / 1_000_000
        reset_after = out[3] / 1_000_000
        if allowed:
            return Decision.allow(
                remaining=remaining,
                reset_after=reset_after,
                scope=self._scope,
                limit="token_bucket",
            )
        return Decision.deny(
            retry_after,
            remaining=remaining,
            reset_after=reset_after,
            scope=self._scope,
            limit="token_bucket",
        )

    async def refund(self, cost: float = 1.0) -> None:
        """Return ``cost`` tokens (clamped to capacity) — the inverse of an admit.

        Used by the hierarchy to roll back a consumption when a more-restrictive
        sibling denies, and by reservations that don't get used. Deferred import
        of the refund unit avoids a quota<->algorithm import cycle.
        """
        from app.throttle.quota import TOKEN_BUCKET_REFUND_UNIT

        await self._transport.run(
            TOKEN_BUCKET_REFUND_UNIT,
            [self._key],
            [self._config.capacity, cost],
        )


__all__ = [
    "TOKEN_BUCKET_UNIT",
    "TokenBucketConfig",
    "TokenBucketLimiter",
]
