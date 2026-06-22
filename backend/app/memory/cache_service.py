"""Content-hash shot cache — why a re-read costs nothing (kinora.md §8.7).

The cache is keyed by ``shot_hash`` (see :func:`app.db.hashing.compute_shot_hash`),
which folds in the ``reference_set_hash``. A **hit** serves the cached clip from
object storage and spends **zero** video-seconds; a **miss** signals the render
path to reserve budget and enqueue. Because the hash includes the reference set,
a Director edit that changes one character only invalidates the shots that
depend on that character — everything else still hits.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel

from app.db.hashing import compute_shot_hash
from app.db.models.shot import ShotCache
from app.db.repositories.shot import ShotCacheRepo
from app.memory.interfaces import BlobStore

# Unit separator: cannot appear in entity-id text, so the join is unambiguous.
_SEP = "\x1f"


class CacheLookup(BaseModel):
    """The outcome of a cache probe (hit serves a clip at 0 video-seconds)."""

    hit: bool
    shot_hash: str
    reference_set_hash: str
    clip_key: str | None = None
    clip_url: str | None = None
    last_frame_key: str | None = None
    last_frame_url: str | None = None
    sync_segment: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    #: Video-seconds charged by serving this result — always 0 on a hit.
    video_seconds: float = 0.0


class CacheService:
    """Compute the content hashes and probe / populate the shot cache."""

    def __init__(
        self,
        *,
        cache: ShotCacheRepo,
        blob_store: BlobStore | None = None,
        url_ttl: int = 3600,
    ) -> None:
        self._cache = cache
        self._store = blob_store
        self._ttl = url_ttl

    @staticmethod
    def reference_set_hash(reference_image_ids: list[str]) -> str:
        """Stable hash of a reference set (order-independent), ``sha1:`` prefixed.

        The §8.2 shot record stores this so a shot's identity is tied to *which*
        locked references it used; changing the set changes the shot_hash.
        """
        joined = _SEP.join(sorted(reference_image_ids))
        return "sha1:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def shot_hash(
        self,
        *,
        book_id: str,
        beat_id: str,
        canon_version_at_render: int,
        render_mode: str,
        seed: int,
        reference_set_hash: str,
    ) -> str:
        """The §8.7 idempotency key / cache key for a shot's render inputs."""
        return compute_shot_hash(
            book_id=book_id,
            beat_id=beat_id,
            canon_version_at_render=canon_version_at_render,
            render_mode=render_mode,
            seed=seed,
            reference_set_hash=reference_set_hash,
        )

    async def get(self, shot_hash: str) -> ShotCache | None:
        """Look up a cached clip by content hash."""
        return await self._cache.get(shot_hash)

    async def put(
        self,
        *,
        shot_hash: str,
        book_id: str,
        clip_key: str | None = None,
        last_frame_key: str | None = None,
        sync_segment: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        video_seconds: float | None = None,
    ) -> ShotCache:
        """Insert/replace a cache record (so a future identical render is a hit)."""
        return await self._cache.put(
            shot_hash=shot_hash,
            book_id=book_id,
            clip_key=clip_key,
            last_frame_key=last_frame_key,
            sync_segment=sync_segment,
            qa=qa,
            video_seconds=video_seconds,
        )

    async def check_or_miss(
        self,
        *,
        book_id: str,
        beat_id: str,
        canon_version_at_render: int,
        render_mode: str,
        seed: int,
        reference_image_ids: list[str],
    ) -> CacheLookup:
        """Probe the cache; return a hit (0 video-seconds) or a typed miss."""
        ref_hash = self.reference_set_hash(reference_image_ids)
        digest = self.shot_hash(
            book_id=book_id,
            beat_id=beat_id,
            canon_version_at_render=canon_version_at_render,
            render_mode=render_mode,
            seed=seed,
            reference_set_hash=ref_hash,
        )
        record = await self._cache.get(digest)
        if record is None:
            return CacheLookup(hit=False, shot_hash=digest, reference_set_hash=ref_hash)
        return CacheLookup(
            hit=True,
            shot_hash=digest,
            reference_set_hash=ref_hash,
            clip_key=record.clip_key,
            clip_url=self._presign(record.clip_key),
            last_frame_key=record.last_frame_key,
            last_frame_url=self._presign(record.last_frame_key),
            sync_segment=record.sync_segment,
            qa=record.qa,
            video_seconds=0.0,
        )

    def _presign(self, key: str | None) -> str | None:
        if key is None or self._store is None:
            return None
        return self._store.presigned_get_url(key, ttl=self._ttl)


__all__ = ["CacheLookup", "CacheService"]
