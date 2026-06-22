"""Repository for render-queue jobs (kinora.md §12.1)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.base import new_id
from app.db.models.enums import RenderJobStatus, RenderPriority
from app.db.models.render_job import RenderJob
from app.db.repositories.base import BaseRepository

# Queue states that count as "in flight" (occupying a concurrency lane).
_INFLIGHT = (
    RenderJobStatus.QUEUED,
    RenderJobStatus.RESERVED,
    RenderJobStatus.SUBMITTED,
    RenderJobStatus.POLLING,
    RenderJobStatus.RETRYING,
)


class RenderJobRepo(BaseRepository):
    """Create render jobs and drive their queue state machine."""

    async def create(
        self,
        *,
        priority: RenderPriority,
        session_id: str | None = None,
        shot_id: str | None = None,
        shot_hash: str | None = None,
        status: RenderJobStatus = RenderJobStatus.QUEUED,
        cancel_token: str | None = None,
        reserved_video_s: float = 0.0,
        attempts: int = 0,
        provider_task_id: str | None = None,
        job_id: str | None = None,
    ) -> RenderJob:
        """Enqueue a render job."""
        job = RenderJob(
            id=job_id or new_id(),
            session_id=session_id,
            shot_id=shot_id,
            shot_hash=shot_hash,
            priority=priority,
            status=status,
            cancel_token=cancel_token,
            reserved_video_s=reserved_video_s,
            attempts=attempts,
            provider_task_id=provider_task_id,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: str) -> RenderJob | None:
        """Fetch a render job by id."""
        return await self.session.get(RenderJob, job_id)

    async def update(self, job_id: str, **fields: Any) -> RenderJob | None:
        """Patch arbitrary job columns; returns the row (or ``None``)."""
        job = await self.session.get(RenderJob, job_id)
        if job is None:
            return None
        for key, value in fields.items():
            setattr(job, key, value)
        await self.session.flush()
        return job

    async def set_status(self, job_id: str, status: RenderJobStatus) -> RenderJob | None:
        """Transition a job to ``status``."""
        return await self.update(job_id, status=status)

    async def increment_attempts(self, job_id: str) -> RenderJob | None:
        """Bump the retry counter (used by the backoff/DLQ logic)."""
        job = await self.session.get(RenderJob, job_id)
        if job is None:
            return None
        job.attempts = job.attempts + 1
        await self.session.flush()
        return job

    async def list_inflight(self, *, session_id: str | None = None) -> list[RenderJob]:
        """Return jobs currently occupying a lane, optionally scoped to a session."""
        stmt = select(RenderJob).where(RenderJob.status.in_(_INFLIGHT))
        if session_id is not None:
            stmt = stmt.where(RenderJob.session_id == session_id)
        stmt = stmt.order_by(RenderJob.created_at)
        return list((await self.session.execute(stmt)).scalars().all())
