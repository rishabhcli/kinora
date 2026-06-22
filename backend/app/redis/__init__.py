"""Async Redis client: JSON get/set, a safe distributed lock, and pub/sub.

Backs the render queue, scheduler session state, SSE/WS fan-out, and the
in-flight ``shot_hash`` dedup locks (kinora.md §12.1–12.3). Note: this package
is ``app.redis``; the third-party driver is the top-level ``redis`` package and
is imported absolutely inside :mod:`app.redis.client` (no shadowing).
"""

from __future__ import annotations

from app.redis.client import DistributedLock, LockNotAcquiredError, RedisClient

__all__ = ["DistributedLock", "LockNotAcquiredError", "RedisClient"]
