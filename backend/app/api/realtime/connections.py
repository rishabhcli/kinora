"""Connection lifecycle registry for SSE/WS at scale (kinora.md §5.6, §12).

A long-running adaptation keeps a stream open for the whole read; across replicas
and reconnects we need to *bound* that. This registry answers three operational
questions an in-memory ``set`` of websockets cannot:

* **How many live connections does this session / user have?** (caps + the
  presence count) — across all API replicas, so it lives in Redis, not process
  memory.
* **Is a session over its connection cap?** — refuse the N+1 connection rather
  than let one reader (or a reconnect storm) open hundreds of streams.
* **Which connections went stale?** — a crashed client never sends a close
  frame; its registry entry is TTL'd and reaped by the background sweeper so the
  presence count doesn't drift upward forever.

Each connection is a short-lived registry entry: a member of a per-session Redis
sorted set scored by its last-heartbeat timestamp. A live stream heartbeats
(re-scores itself) on the same cadence it pings the client; the sweeper drops
entries whose score is older than the stale threshold. Counts are derived by
counting non-stale members, so a missed close is self-healing.

The registry is a context manager (:meth:`connection`) so a route's
``async with registry.connection(...) as conn:`` guarantees deregistration on
every exit path, and fails *open* (admits the connection, no bookkeeping) when
Redis is down so a cache blip never refuses a reader their stream.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.api.realtime.connections")

#: Default ceiling on concurrent connections per (session, transport). Generous
#: enough for a few tabs + a reconnect overlap; low enough to refuse a storm.
DEFAULT_MAX_PER_SESSION = 8
#: Default ceiling per user across all their sessions (abuse / runaway guard).
DEFAULT_MAX_PER_USER = 32
#: A connection whose heartbeat is older than this is considered dead + reapable.
DEFAULT_STALE_AFTER_S = 60.0
#: Registry entry TTL — a safety net beyond the sweeper (a whole replica dying
#: still lets its sets expire). Comfortably > stale threshold.
DEFAULT_TTL_S = 300


class ConnectionLimitExceeded(Exception):  # noqa: N818 - reads as a cap-breach signal
    """Raised when admitting a connection would breach a cap (mapped to 429)."""

    def __init__(self, scope: str, limit: int) -> None:
        super().__init__(f"connection cap reached for {scope}")
        self.scope = scope
        self.limit = limit


@dataclass(slots=True)
class ConnectionHandle:
    """A live registration; heartbeat it to stay counted, close to deregister."""

    conn_id: str
    session_id: str
    user_id: str
    _registry: ConnectionRegistry
    closed: bool = field(default=False)

    async def heartbeat(self) -> None:
        """Refresh this connection's liveness (call on the stream's ping cadence)."""
        if not self.closed:
            await self._registry._touch(self.session_id, self.user_id, self.conn_id)

    async def close(self) -> None:
        """Deregister this connection (idempotent)."""
        if not self.closed:
            self.closed = True
            await self._registry._remove(self.session_id, self.user_id, self.conn_id)


class ConnectionRegistry:
    """Redis-backed, replica-spanning connection bookkeeping with caps + reaping."""

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:conn",
        max_per_session: int = DEFAULT_MAX_PER_SESSION,
        max_per_user: int = DEFAULT_MAX_PER_USER,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._max_session = max_per_session
        self._max_user = max_per_user
        self._stale_after_ms = stale_after_s * 1000.0
        self._ttl_ms = max(int(ttl_s * 1000), 1000)

    # -- keys ---------------------------------------------------------------- #

    def _session_key(self, session_id: str) -> str:
        return f"{self._ns}:session:{session_id}"

    def _user_key(self, user_id: str) -> str:
        return f"{self._ns}:user:{user_id}"

    def _index_key(self) -> str:
        return f"{self._ns}:sessions"

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    @property
    def max_per_session(self) -> int:
        """The per-session connection cap (surfaced by the stats endpoint)."""
        return self._max_session

    @property
    def max_per_user(self) -> int:
        """The per-user connection cap."""
        return self._max_user

    # -- counting ------------------------------------------------------------ #

    async def _live_count(self, key: str) -> int:
        """Members heartbeated within the stale window (the live count)."""
        floor = self._now_ms() - self._stale_after_ms
        try:
            return int(await self._redis.zcount(key, floor, "+inf"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("connections.count_failed", key=key, error=str(exc))
            return 0

    async def session_count(self, session_id: str) -> int:
        """Live connection count for a session (the presence headcount)."""
        return await self._live_count(self._session_key(session_id))

    async def user_count(self, user_id: str) -> int:
        """Live connection count across all of a user's sessions."""
        return await self._live_count(self._user_key(user_id))

    # -- lifecycle ----------------------------------------------------------- #

    async def _touch(self, session_id: str, user_id: str, conn_id: str) -> None:
        now = self._now_ms()
        with contextlib.suppress(Exception):
            session_key = self._session_key(session_id)
            user_key = self._user_key(user_id)
            await self._redis.zadd(session_key, {conn_id: now})
            await self._redis.zadd(user_key, {conn_id: now})
            await self._redis.pexpire(session_key, self._ttl_ms)
            await self._redis.pexpire(user_key, self._ttl_ms)
            await self._redis.sadd(self._index_key(), session_id)

    async def _remove(self, session_id: str, user_id: str, conn_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._redis.zrem(self._session_key(session_id), conn_id)
            await self._redis.zrem(self._user_key(user_id), conn_id)
            if await self._redis.zcard(self._session_key(session_id)) == 0:
                await self._redis.srem(self._index_key(), session_id)

    @contextlib.asynccontextmanager
    async def connection(
        self, *, session_id: str, user_id: str
    ) -> AsyncIterator[ConnectionHandle]:
        """Register a connection for the body's duration, enforcing caps.

        Raises :class:`ConnectionLimitExceeded` *before* admitting if a cap is
        breached. Fails open (admits, no bookkeeping) on Redis errors.
        """
        # Sweep stale members first so a cap check isn't polluted by dead entries.
        await self._reap_session(session_id)
        try:
            session_n = await self.session_count(session_id)
            user_n = await self.user_count(user_id)
        except Exception:  # noqa: BLE001 - fail open on a count error
            session_n = user_n = 0
        if session_n >= self._max_session:
            raise ConnectionLimitExceeded(f"session:{session_id}", self._max_session)
        if user_n >= self._max_user:
            raise ConnectionLimitExceeded(f"user:{user_id}", self._max_user)

        conn_id = uuid.uuid4().hex
        handle = ConnectionHandle(
            conn_id=conn_id, session_id=session_id, user_id=user_id, _registry=self
        )
        await self._touch(session_id, user_id, conn_id)
        logger.info("connections.open", session_id=session_id, conn_id=conn_id, count=session_n + 1)
        try:
            yield handle
        finally:
            await handle.close()
            logger.info("connections.close", session_id=session_id, conn_id=conn_id)

    # -- reaping (the sweeper's worker) -------------------------------------- #

    async def _reap_session(self, session_id: str) -> int:
        floor = self._now_ms() - self._stale_after_ms
        removed = 0
        with contextlib.suppress(Exception):
            removed = int(
                await self._redis.zremrangebyscore(self._session_key(session_id), "-inf", floor)
            )
            if await self._redis.zcard(self._session_key(session_id)) == 0:
                await self._redis.srem(self._index_key(), session_id)
        return removed

    async def reap_all(self) -> int:
        """Drop stale members across every tracked session (the sweeper tick).

        Iterates the session index, trims dead members from each, and prunes the
        per-user sets opportunistically. Returns the number reaped.
        """
        total = 0
        try:
            session_ids = await self._redis.smembers(self._index_key())
        except Exception as exc:  # noqa: BLE001
            logger.warning("connections.reap_index_failed", error=str(exc))
            return 0
        for session_id in session_ids:
            total += await self._reap_session(session_id)
        # Per-user sets carry their own TTL, so abandoned ones expire on their own;
        # the sweeper only needs to keep the session sets (the presence count) clean.
        if total:
            logger.info("connections.reaped", count=total)
        return total

    async def tracked_sessions(self) -> list[str]:
        """Session ids currently in the index (diagnostics / sweeper input)."""
        try:
            return list(await self._redis.smembers(self._index_key()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("connections.tracked_failed", error=str(exc))
            return []


__all__ = [
    "DEFAULT_MAX_PER_SESSION",
    "DEFAULT_MAX_PER_USER",
    "DEFAULT_STALE_AFTER_S",
    "ConnectionHandle",
    "ConnectionLimitExceeded",
    "ConnectionRegistry",
]
