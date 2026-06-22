"""The cheap keyframe lane — one still per beat, **zero video-seconds** (§4.4).

The speculative zone is represented by a single image-generated keyframe per
beat, not video. :class:`KeyframeService` ensures that still exists in object
storage (generating it with the image model, or serving an already-cached one)
and publishes a ``keyframe_ready`` event (§5.6) so the client can Ken-Burns over
it as an instant bridge. By construction this service has **no budget
dependency** — it can never draw down the 1,650 video-seconds. The image is
keyed by beat (the keyframe cache, §12.3), so a re-visit re-uses it for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import anyio

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.memory.interfaces import BlobStore
from app.providers.errors import ProviderError
from app.queue.redis_queue import book_channel, session_channel
from app.storage.object_store import keys

logger = get_logger("app.scheduler.keyframe")


class ImageGen(Protocol):
    """The slice of the image provider the keyframe lane needs (§9.1)."""

    async def generate(self, prompt: str, *, n: int = 1) -> list[bytes]: ...


class EventPublisher(Protocol):
    """The pub/sub publish surface (satisfied by :class:`RedisClient`)."""

    async def publish(self, channel: str, message: Any) -> int: ...


@dataclass(frozen=True, slots=True)
class KeyframeResult:
    """The outcome of :meth:`KeyframeService.ensure_keyframe`."""

    beat_id: str
    key: str
    oss_url: str | None
    cache_hit: bool
    #: Always 0.0 — the keyframe lane spends no video budget (§4.4).
    video_seconds: float = 0.0


class KeyframeService:
    """Ensure a beat's keyframe still exists and announce it (no video-seconds)."""

    def __init__(
        self,
        *,
        image: ImageGen,
        object_store: BlobStore,
        redis: EventPublisher | None = None,
        settings: Settings | None = None,
        url_ttl: int = 3600,
    ) -> None:
        self._image = image
        self._store = object_store
        self._redis = redis
        self._settings = settings or get_settings()
        self._ttl = url_ttl

    async def ensure_keyframe(
        self,
        book_id: str,
        beat_id: str,
        *,
        prompt: str | None = None,
        session_id: str | None = None,
    ) -> KeyframeResult:
        """Generate (or reuse) the beat's keyframe and publish ``keyframe_ready``.

        A cache hit re-uses the stored still and spends nothing; a miss generates
        one still via the image model. Either way the cost against the video
        budget is **zero**.
        """
        if not beat_id:
            raise ValueError("ensure_keyframe requires a beat_id")
        key = keys.keyframe(book_id, beat_id)

        if await self._exists(key):
            url = await self._presign(key)
            await self._announce(beat_id, url, session_id=session_id, book_id=book_id)
            logger.info("keyframe.cache_hit", book_id=book_id, beat_id=beat_id)
            return KeyframeResult(beat_id=beat_id, key=key, oss_url=url, cache_hit=True)

        text = prompt or f"storybook keyframe still for beat {beat_id}"
        try:
            images = await self._image.generate(text, n=1)
        except ProviderError as exc:
            logger.warning("keyframe.gen_failed", beat_id=beat_id, error=str(exc))
            raise
        if not images:
            raise ProviderError(f"image model returned no keyframe for beat {beat_id}")

        await self._put(key, images[0], "image/png")
        url = await self._presign(key)
        await self._announce(beat_id, url, session_id=session_id, book_id=book_id)
        logger.info("keyframe.generated", book_id=book_id, beat_id=beat_id, key=key)
        return KeyframeResult(beat_id=beat_id, key=key, oss_url=url, cache_hit=False)

    async def _announce(
        self, beat_id: str, oss_url: str | None, *, session_id: str | None, book_id: str
    ) -> None:
        if self._redis is None:
            return
        channel = session_channel(session_id) if session_id else book_channel(book_id)
        await self._redis.publish(
            channel, {"event": "keyframe_ready", "beat_id": beat_id, "oss_url": oss_url}
        )

    # -- object-store async wrappers (boto3 is sync) ------------------------- #

    async def _exists(self, key: str) -> bool:
        return await anyio.to_thread.run_sync(self._store.exists, key)

    async def _put(self, key: str, data: bytes, content_type: str) -> None:
        await anyio.to_thread.run_sync(self._store.put_bytes, key, data, content_type)

    async def _presign(self, key: str) -> str:
        return await anyio.to_thread.run_sync(
            lambda: self._store.presigned_get_url(key, ttl=self._ttl)
        )


__all__ = ["EventPublisher", "ImageGen", "KeyframeResult", "KeyframeService"]
