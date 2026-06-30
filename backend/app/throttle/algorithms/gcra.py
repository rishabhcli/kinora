"""Distributed **GCRA** (Generic Cell Rate Algorithm) — leaky-bucket as a virtual
scheduler, the most memory-frugal of the three.

GCRA stores a single number per scope: the **theoretical arrival time** (TAT) —
the earliest moment at which the *next* request would be perfectly conforming.
From two parameters:

* ``emission_interval`` (``T``) = the spacing one request "costs" the schedule,
  i.e. ``1 / rate`` seconds; and
* ``tolerance`` (``tau``) = how much burst is allowed, i.e. ``(burst - 1) * T``;

a request at ``now`` is conforming iff ``now >= tat - tau``. If so the new TAT
becomes ``max(now, tat) + cost * T`` and the request is admitted; otherwise the
exact wait is ``(tat - tau) - now`` — the moment the bucket has leaked enough.

Why GCRA when we already have a token bucket? They are duals — a token bucket
with capacity ``B`` and rate ``R`` is equivalent to GCRA with ``T = 1/R`` and
``tau = (B-1)*T`` — but GCRA needs **O(1) state of a single float** (no token
count to refill, no log of timestamps), which makes it the cheapest option for a
huge fan-out of fine-grained scopes (e.g. per-(tenant,endpoint)). It is also the
algorithm most CDNs/load-balancers implement, so its behaviour matches what
upstream providers themselves enforce. We expose it as the leaky-bucket lane.

**State** is one string key (the TAT) with a TTL of ``tat - now`` (it is
meaningless once fully leaked). Lua and ``apply`` are equivalent; tests assert
the conforming/non-conforming boundary, burst tolerance, and retry-after.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.throttle.result import Decision
from app.throttle.transport import ComputeUnit, Store, Transport, UnitResult

_GCRA_LUA = """
-- KEYS[1] = TAT key
-- ARGV[1], ARGV[2] = server time (secs, micros)
-- ARGV[3] = emission_interval T, ARGV[4] = tolerance tau, ARGV[5] = cost
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local T = tonumber(ARGV[3])
local tau = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])

local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil then tat = now end

local increment = cost * T
local allowed = 0
local retry_after = 0.0
local new_tat = math.max(tat, now) + increment
-- Canonical GCRA: a request conforms iff now is at or past (tat - tau).
if now >= (tat - tau) - 1e-9 then
  allowed = 1
  redis.call('SET', KEYS[1], new_tat)
  local ttl_ms = math.ceil((new_tat - now) * 1000)
  if ttl_ms < 1 then ttl_ms = 1 end
  redis.call('PEXPIRE', KEYS[1], ttl_ms)
else
  retry_after = (tat - tau) - now
  if retry_after < 0 then retry_after = 0 end
end

local reset_after = tat - now
if reset_after < 0 then reset_after = 0 end
return {allowed, math.floor(retry_after * 1000000), math.floor(reset_after * 1000000)}
"""

_EPSILON = 1e-9


def _gcra_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    emission_interval = args[0]  # T
    tolerance = args[1]  # tau
    cost = args[2]

    tat = store.get(key)
    if tat is None:
        tat = now

    increment = cost * emission_interval
    allowed = 0.0
    retry_after = 0.0
    # Conforming iff now is at or past the earliest allowed arrival (tat - tau).
    if now >= (tat - tolerance) - _EPSILON:
        allowed = 1.0
        new_tat = max(tat, now) + increment
        store.set(key, new_tat, ttl_s=max(0.001, new_tat - now))
        tat = new_tat
    else:
        retry_after = max(0.0, (tat - tolerance) - now)

    reset_after = max(0.0, tat - now)
    return [allowed, retry_after * 1_000_000, reset_after * 1_000_000]


GCRA_UNIT = ComputeUnit(
    name="gcra",
    lua=_GCRA_LUA,
    key_count=1,
    apply=_gcra_apply,
)


@dataclass(frozen=True, slots=True)
class GcraConfig:
    """Leaky-bucket via GCRA: sustain ``rate`` req/s with bursts up to ``burst``.

    Internally ``T = 1 / rate`` and ``tau = (burst - 1) * T``. ``burst=1`` is a
    pure metronome (no burst); ``burst>1`` permits that many back-to-back before
    spacing kicks in.
    """

    rate: float
    burst: int = 1

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be > 0")
        if self.burst < 1:
            raise ValueError("burst must be >= 1")

    @property
    def emission_interval(self) -> float:
        return 1.0 / self.rate

    @property
    def tolerance(self) -> float:
        return (self.burst - 1) * self.emission_interval


class GcraLimiter:
    """A distributed GCRA / leaky-bucket limiter over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        scope: str,
        config: GcraConfig,
        *,
        key_prefix: str = "throttle:gcra",
    ) -> None:
        self._transport = transport
        self._scope = scope
        self._config = config
        self._key = f"{key_prefix}:{scope}"

    @property
    def scope(self) -> str:
        return self._scope

    async def check(self, cost: float = 1.0) -> Decision:
        """Atomically test conformance and (if conforming) advance the TAT."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        out = await self._transport.run(
            GCRA_UNIT,
            [self._key],
            [self._config.emission_interval, self._config.tolerance, cost],
        )
        allowed = out[0] >= 0.5
        retry_after = out[1] / 1_000_000
        reset_after = out[2] / 1_000_000
        if allowed:
            return Decision.allow(
                reset_after=reset_after,
                scope=self._scope,
                limit="gcra",
            )
        return Decision.deny(
            retry_after,
            reset_after=reset_after,
            scope=self._scope,
            limit="gcra",
        )

    async def refund(self, cost: float = 1.0) -> None:
        """Rewind the TAT by ``cost * T`` — the inverse of a conforming admit."""
        from app.throttle.quota import GCRA_REFUND_UNIT

        await self._transport.run(
            GCRA_REFUND_UNIT,
            [self._key],
            [self._config.emission_interval, cost],
        )


__all__ = [
    "GCRA_UNIT",
    "GcraConfig",
    "GcraLimiter",
]
