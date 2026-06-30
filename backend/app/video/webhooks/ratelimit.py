"""A dependency-free, per-key token-bucket rate limiter for the ingress.

The callback route is **unauthenticated** (the signature is the auth), so it is
exposed to whoever can reach it. Beyond the size guard we throttle per source so
a misbehaving (or hostile) caller can't hammer the verifier. This is a tiny
in-process token bucket — no Redis required — so the subsystem stays testable in
isolation; the shared Redis limiter (``app.api.deps.RateLimiter``) can front the
route additionally in production for cross-process fairness.

Keyed by the source identity the route picks (client IP, or provider+IP). Fails
*closed* only when explicitly over budget; an unknown key starts full.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketRateLimiter:
    """Per-key token bucket: ``capacity`` tokens, refilled ``refill_per_s``."""

    def __init__(
        self,
        *,
        capacity: int = 120,
        refill_per_s: float = 4.0,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill_per_s must be positive")
        self._capacity = float(capacity)
        self._refill = refill_per_s
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """Spend ``cost`` tokens for ``key``; return ``False`` if it would go negative."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, updated=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill)
            bucket.updated = now
        self._buckets.move_to_end(key)
        self._evict()
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True
        return False

    def _evict(self) -> None:
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)


__all__ = ["TokenBucketRateLimiter"]
