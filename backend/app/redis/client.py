"""Async Redis client wrapper.

Provides:

* JSON-typed ``get_json`` / ``set_json`` helpers,
* a context-managed :class:`DistributedLock` (``SET NX PX`` acquire + a Lua
  compare-and-delete release so a lock is only released by its owner), and
* ``publish`` / ``subscribe`` pub-sub helpers.

The third-party driver (top-level ``redis``) is intentionally typed loosely as
``Any`` internally because the installed ``redis`` runtime and the ``types-redis``
stubs can drift; the public surface of this module is fully typed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.core.config import get_settings

# Release only if we still own the lock (atomic compare-and-delete).
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class LockNotAcquiredError(RuntimeError):
    """Raised when a :class:`DistributedLock` cannot be acquired in time."""


class DistributedLock:
    """A Redis ``SET NX PX`` lock with safe, owner-only release.

    Usable as ``async with redis_client.lock("name") as lock:`` (acquires on
    enter, releases on exit) or imperatively via :meth:`acquire` / :meth:`release`.
    """

    def __init__(
        self,
        redis: Any,
        name: str,
        *,
        ttl_ms: int = 10_000,
        token: str | None = None,
        blocking: bool = True,
        blocking_timeout: float = 5.0,
        retry_interval: float = 0.05,
    ) -> None:
        self._redis = redis
        self._name = name
        self._ttl_ms = ttl_ms
        self._token = token or uuid.uuid4().hex
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._retry_interval = retry_interval
        self._acquired = False

    @property
    def token(self) -> str:
        """The unique owner token written into the lock key."""
        return self._token

    @property
    def acquired(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._acquired

    async def acquire(self) -> bool:
        """Attempt a single non-blocking acquire; returns success."""
        ok = await self._redis.set(self._name, self._token, nx=True, px=self._ttl_ms)
        self._acquired = bool(ok)
        return self._acquired

    async def release(self) -> bool:
        """Release the lock iff we still own it; returns whether a key was deleted."""
        deleted = await self._redis.eval(_RELEASE_LUA, 1, self._name, self._token)
        self._acquired = False
        return bool(deleted)

    async def __aenter__(self) -> DistributedLock:
        deadline = time.monotonic() + self._blocking_timeout
        while True:
            if await self.acquire():
                return self
            if not self._blocking or time.monotonic() >= deadline:
                raise LockNotAcquiredError(f"could not acquire lock {self._name!r}")
            await asyncio.sleep(self._retry_interval)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


class RedisClient:
    """Async wrapper over a :class:`redis.asyncio.Redis` connection."""

    def __init__(self, client: Redis) -> None:
        self._redis: Any = client

    @classmethod
    def from_url(cls, url: str | None = None) -> RedisClient:
        """Build a client from ``url`` (or ``settings.redis_url``)."""
        target = url or get_settings().redis_url
        client = Redis.from_url(target, decode_responses=True)
        return cls(client)

    @property
    def raw(self) -> Any:
        """The underlying redis-py async client (escape hatch)."""
        return self._redis

    async def ping(self) -> bool:
        """Round-trip ping (readiness check)."""
        return bool(await self._redis.ping())

    async def close(self) -> None:
        """Close the connection pool."""
        await self._redis.aclose()

    # --- typed JSON helpers ---

    async def get_json(self, key: str) -> Any | None:
        """Get and JSON-decode ``key`` (``None`` if absent)."""
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        """JSON-encode ``value`` and set it at ``key`` (optional TTL in seconds)."""
        payload = json.dumps(value, separators=(",", ":"))
        if ttl_s is not None:
            await self._redis.set(key, payload, ex=ttl_s)
        else:
            await self._redis.set(key, payload)

    async def delete(self, *keys: str) -> int:
        """Delete one or more keys; returns the number removed."""
        if not keys:
            return 0
        return int(await self._redis.delete(*keys))

    # --- pub/sub ---

    async def publish(self, channel: str, message: Any) -> int:
        """JSON-encode and publish ``message`` to ``channel``; returns subscriber count."""
        payload = json.dumps(message, separators=(",", ":"))
        return int(await self._redis.publish(channel, payload))

    @asynccontextmanager
    async def subscribe(self, *channels: str) -> AsyncIterator[PubSub]:
        """Subscribe to ``channels`` for the duration of the context."""
        pubsub: Any = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()

    async def next_message(self, pubsub: PubSub, *, timeout: float = 5.0) -> Any | None:
        """Wait up to ``timeout`` for the next real (non-subscribe) JSON message.

        Returns the decoded payload, or ``None`` if the timeout elapses.
        """
        deadline = time.monotonic() + timeout
        ps: Any = pubsub
        while time.monotonic() < deadline:
            message = await ps.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if message is not None and message.get("type") == "message":
                data = message["data"]
                return json.loads(data)
        return None

    # --- locks ---

    def lock(
        self,
        name: str,
        *,
        ttl_ms: int = 10_000,
        token: str | None = None,
        blocking: bool = True,
        blocking_timeout: float = 5.0,
    ) -> DistributedLock:
        """Build a :class:`DistributedLock` bound to this client."""
        return DistributedLock(
            self._redis,
            name,
            ttl_ms=ttl_ms,
            token=token,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )
