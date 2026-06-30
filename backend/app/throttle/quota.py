"""Quota **reservation, refund, and borrowing** — making consumption reversible.

Two problems this layer solves:

**1. Hierarchical rollback.** When several limits are enforced together
(:mod:`app.throttle.hierarchy`), we admit by consuming from each in order. If a
*later*, more-restrictive limit denies, the earlier limits already consumed — and
a token bucket with no refund would leak that capacity permanently, slowly
starving everyone. So every algorithm here exposes a **refund**: return ``cost``
to the scope as if it were never taken. For the token bucket and GCRA this is the
exact inverse op (add tokens / rewind the TAT); for the sliding-window log it
removes the members just added. The hierarchy uses this to make a multi-limit
``acquire`` *all-or-nothing*.

**2. Reservation borrowing/refund for speculative work.** The Kinora scheduler
*reserves* video-seconds it might not use (speculative shots that get evicted
before render). A reservation is a quota debit you can later **commit** (the work
ran) or **refund** (it didn't). Borrowing lets a scope temporarily exceed its
steady rate against a bounded *overdraft*, repaid by future idle capacity — the
"can I front you a few tokens now if you'll be quiet later" pattern that keeps a
bursty-but-low-average workload from being throttled on its bursts.

All operations are atomic compute-units mirroring the limiter state, so a refund
is as crash-safe and cross-process-consistent as the original debit.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.throttle.transport import ComputeUnit, Store, UnitResult

# --------------------------------------------------------------------------- #
# Refund unit for the token bucket (and, by duality, GCRA via rewind)
# --------------------------------------------------------------------------- #

_TB_REFUND_LUA = """
-- KEYS[1] = bucket hash; ARGV[1,2] = server time; ARGV[3] = capacity; ARGV[4] = refund
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local capacity = tonumber(ARGV[3])
local refund = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
if tokens == nil then
  -- Nothing to refund into a non-existent bucket beyond capacity.
  tokens = capacity
else
  tokens = math.min(capacity, tokens + refund)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
return {math.floor(tokens * 1000000)}
"""


def _tb_refund_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    capacity, refund = args[0], args[1]
    state = store.hgetall(key)
    tokens = state.get("tokens")
    tokens = capacity if tokens is None else min(capacity, tokens + refund)
    store.hset(key, {"tokens": tokens, "ts": now})
    return [tokens * 1_000_000]


TOKEN_BUCKET_REFUND_UNIT = ComputeUnit(
    name="token_bucket_refund",
    lua=_TB_REFUND_LUA,
    key_count=1,
    apply=_tb_refund_apply,
)

# --------------------------------------------------------------------------- #
# GCRA refund (rewind the TAT by cost * T)
# --------------------------------------------------------------------------- #

_GCRA_REFUND_LUA = """
-- KEYS[1] = TAT key; ARGV[1,2] = server time; ARGV[3] = T; ARGV[4] = cost
local now = tonumber(ARGV[1]) + tonumber(ARGV[2]) / 1000000.0
local T = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil then return {0} end
tat = tat - cost * T
if tat < now then tat = now end
redis.call('SET', KEYS[1], tat)
return {math.floor(tat * 1000000)}
"""


def _gcra_refund_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    emission_interval, cost = args[0], args[1]
    tat = store.get(key)
    if tat is None:
        return [0.0]
    tat = max(now, tat - cost * emission_interval)
    store.set(key, tat)
    return [tat * 1_000_000]


GCRA_REFUND_UNIT = ComputeUnit(
    name="gcra_refund",
    lua=_GCRA_REFUND_LUA,
    key_count=1,
    apply=_gcra_refund_apply,
)

# --------------------------------------------------------------------------- #
# Sliding-window refund (remove the members a prior admission added)
# --------------------------------------------------------------------------- #

_SW_REFUND_LUA = """
-- KEYS[1] = window zset; ARGV[1] = cost; ARGV[2] = id base
local cost = tonumber(ARGV[1])
local idbase = ARGV[2]
local removed = 0
for i = 1, cost do
  removed = removed + redis.call('ZREM', KEYS[1], idbase .. ':' .. i)
end
return {removed}
"""


def _sw_refund_apply(store: Store, keys: list[str], args: list[float], now: float) -> UnitResult:
    key = keys[0]
    cost = int(args[0])
    id_seed = int(args[1])
    removed = 0
    for i in range(cost):
        removed += store.zrem(key, f"{id_seed}:{i}")
    return [float(removed)]


SLIDING_WINDOW_REFUND_UNIT = ComputeUnit(
    name="sliding_window_refund",
    lua=_SW_REFUND_LUA,
    key_count=1,
    apply=_sw_refund_apply,
)


# --------------------------------------------------------------------------- #
# Reservation handle — commit vs refund speculative debits
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Reservation:
    """A consumed-but-reversible debit against one scope.

    Returned by a limiter's reserve path; the holder later calls
    :meth:`ReservationLedger.commit` (the work happened — keep the debit) or
    :meth:`ReservationLedger.refund` (it didn't — give it back). Modelled as a
    plain value object so it can be persisted / passed between coroutines; the
    ledger does the atomic store op.
    """

    scope: str
    cost: float
    limit_kind: str
    #: Algorithm-specific reversal token (e.g. the sliding-window member seed).
    refund_token: float = 0.0
    #: Set once resolved so a double commit/refund is a caught programming error.
    resolved: bool = False


class QuotaError(RuntimeError):
    """A reservation was committed/refunded twice, or against the wrong ledger."""


__all__ = [
    "GCRA_REFUND_UNIT",
    "SLIDING_WINDOW_REFUND_UNIT",
    "TOKEN_BUCKET_REFUND_UNIT",
    "QuotaError",
    "Reservation",
]
