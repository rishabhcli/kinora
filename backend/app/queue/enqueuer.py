"""``RedisRenderEnqueuer`` — the memory-layer render seam (kinora.md §8.3/§12.1).

Phase 4 declared a :class:`app.memory.interfaces.RenderEnqueuer` protocol and
shipped a :class:`NotWired` default; this is the real implementation Phase 9's
MCP ``shot.render`` injects. It translates a :class:`ShotSpec` into a durable,
idempotent queue job and returns the ``job_id``.

The MCP tool probes the shot cache *before* calling (a hit spends zero
video-seconds), but the enqueue is idempotent on ``shot_hash`` regardless, so a
duplicate call is collapsed to the existing job rather than double-spending the
budget (§12.3).
"""

from __future__ import annotations

from app.db.base import new_id
from app.db.hashing import compute_shot_hash
from app.db.models.enums import RenderPriority
from app.memory.cache_service import CacheService
from app.memory.interfaces import ShotSpec
from app.queue.redis_queue import RedisRenderQueue


class RedisRenderEnqueuer:
    """A :class:`RenderEnqueuer` that delegates to the Redis priority queue."""

    def __init__(self, queue: RedisRenderQueue) -> None:
        self._queue = queue

    async def enqueue(
        self,
        shot_spec: ShotSpec,
        priority: RenderPriority,
        cancel_token: str | None = None,
    ) -> str:
        """Enqueue ``shot_spec`` and return the render ``job_id``.

        Returns the existing job's id when the shot is already known/in-flight
        (idempotency), or an empty string when a *speculative* enqueue is dropped
        under backpressure — the keyframe ladder covers a dropped speculation
        (§4.4/§12.2). Committed enqueues are always admitted.
        """
        shot_hash = shot_spec.shot_hash or self._compute_hash(shot_spec)
        result = await self._queue.enqueue(
            shot_hash=shot_hash,
            priority=priority,
            book_id=shot_spec.book_id,
            job_id=new_id(),
            shot_id=shot_spec.shot_id,
            beat_id=shot_spec.beat_id,
            scene_id=shot_spec.scene_id,
            cancel_token=cancel_token,
            target_duration_s=shot_spec.target_duration_s,
            prompt=shot_spec.prompt or None,
        )
        return result.job_id or ""

    @staticmethod
    def _compute_hash(shot_spec: ShotSpec) -> str:
        """Derive the §8.7 content hash when the spec hasn't carried one."""
        ref_hash = shot_spec.reference_set_hash or CacheService.reference_set_hash(
            shot_spec.reference_image_ids
        )
        return compute_shot_hash(
            book_id=shot_spec.book_id,
            beat_id=shot_spec.beat_id,
            canon_version_at_render=shot_spec.canon_version_at_render,
            render_mode=shot_spec.render_mode,
            seed=shot_spec.seed,
            reference_set_hash=ref_hash,
        )


__all__ = ["RedisRenderEnqueuer"]
