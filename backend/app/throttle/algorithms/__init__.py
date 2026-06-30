"""The three distributed limiter algorithms, each an atomic compute-unit.

Pick by trade-off: **token bucket** (bursty, O(1) state, the default for provider
RPS with burst), **sliding-window log** (exact, O(limit) state, when a hard quota
must never be crossed), **GCRA / leaky bucket** (smoothest pacing, O(1) state of a
single float, cheapest for huge scope fan-out). All three return a uniform
:class:`~app.throttle.result.Decision` so the hierarchy combines them freely.
"""

from __future__ import annotations

from app.throttle.algorithms.gcra import (
    GCRA_UNIT,
    GcraConfig,
    GcraLimiter,
)
from app.throttle.algorithms.sliding_window import (
    SLIDING_WINDOW_UNIT,
    SlidingWindowConfig,
    SlidingWindowLimiter,
)
from app.throttle.algorithms.token_bucket import (
    TOKEN_BUCKET_UNIT,
    TokenBucketConfig,
    TokenBucketLimiter,
)

__all__ = [
    "GCRA_UNIT",
    "SLIDING_WINDOW_UNIT",
    "TOKEN_BUCKET_UNIT",
    "GcraConfig",
    "GcraLimiter",
    "SlidingWindowConfig",
    "SlidingWindowLimiter",
    "TokenBucketConfig",
    "TokenBucketLimiter",
]
