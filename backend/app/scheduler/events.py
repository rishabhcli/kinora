"""Session event publishing for the Scheduler control plane (§5.6)."""

from __future__ import annotations

from typing import Any, Protocol

from app.queue.redis_queue import session_channel


class SessionEventPublisher(Protocol):
    """Publish §5.6 events onto a session's Redis pub/sub channel."""

    async def publish(self, session_id: str, message: dict[str, Any]) -> int: ...


class RedisSessionEventPublisher:
    """Real publisher backed by :class:`app.redis.client.RedisClient`."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, session_id: str, message: dict[str, Any]) -> int:
        return await self._redis.publish(session_channel(session_id), message)


__all__ = ["RedisSessionEventPublisher", "SessionEventPublisher"]
