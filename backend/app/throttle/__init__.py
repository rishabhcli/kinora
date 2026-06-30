"""Distributed rate-limit fabric: redis-atomic limiters (token bucket /
sliding-window-log / GCRA) as compute-units with an in-memory emulator, a
hierarchical most-restrictive-wins model, fleet-wide concurrency leases, quota
borrow/refund, and a fail-open client with precise Retry-After.
"""

from __future__ import annotations

from app.throttle.algorithms import (
    GcraConfig,
    GcraLimiter,
    SlidingWindowConfig,
    SlidingWindowLimiter,
    TokenBucketConfig,
    TokenBucketLimiter,
)
from app.throttle.client import SleepFn, ThrottleClient, Verdict
from app.throttle.clock import (
    Clock,
    ManualClock,
    MonotonicClock,
    WallClock,
)
from app.throttle.config import (
    FabricSpec,
    LeaseSpec,
    Level,
    LimitSpec,
    build_client,
)
from app.throttle.errors import (
    LeaseUnavailable,
    StoreUnavailable,
    Throttled,
    ThrottleError,
)
from app.throttle.hierarchy import (
    GcraLimit,
    HierarchicalLimiter,
    HierarchyDecision,
    Limit,
    Refunder,
    SlidingWindowLimit,
    TokenBucketLimit,
)
from app.throttle.leases import (
    ConcurrencyLeasePool,
    Lease,
    LeaseConfig,
)
from app.throttle.quota import (
    QuotaError,
    Reservation,
)
from app.throttle.result import Decision
from app.throttle.transport import (
    ComputeUnit,
    InMemoryScriptTransport,
    InMemoryStore,
    RedisScriptTransport,
    Store,
    Transport,
)

__all__ = [
    "Clock",
    "ComputeUnit",
    "ConcurrencyLeasePool",
    "Decision",
    "FabricSpec",
    "GcraConfig",
    "GcraLimit",
    "GcraLimiter",
    "HierarchicalLimiter",
    "HierarchyDecision",
    "InMemoryScriptTransport",
    "InMemoryStore",
    "Lease",
    "LeaseConfig",
    "LeaseSpec",
    "LeaseUnavailable",
    "Level",
    "Limit",
    "LimitSpec",
    "ManualClock",
    "MonotonicClock",
    "QuotaError",
    "RedisScriptTransport",
    "Refunder",
    "Reservation",
    "SleepFn",
    "SlidingWindowConfig",
    "SlidingWindowLimit",
    "SlidingWindowLimiter",
    "Store",
    "StoreUnavailable",
    "ThrottleClient",
    "ThrottleError",
    "Throttled",
    "TokenBucketConfig",
    "TokenBucketLimit",
    "TokenBucketLimiter",
    "Transport",
    "Verdict",
    "WallClock",
    "build_client",
]
