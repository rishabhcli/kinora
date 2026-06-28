"""Render-job inspection over the Postgres mirror (kinora.md §12.1).

The Redis queue is the authoritative mechanism, but every job is mirrored into
the ``render_jobs`` table for durable inspection / forensics. These actions read
that mirror so an operator can audit job history (including completed jobs the
Redis records may have expired), list in-flight jobs, and review a book's
defects (the §9.5/§12.4 degradation log).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from app.cli.errors import not_found
from app.cli.formatting import ago, isoformat, truncate
from app.cli.output import Payload, Table, kv_table
from app.composition import Container
from app.db.models.enums import RenderJobStatus
from app.db.models.render_job import RenderJob
from app.db.repositories.render_job import RenderJobRepo


@dataclass(frozen=True, slots=True)
class JobRow:
    id: str
    priority: str
    status: str
    shot_id: str | None
    session_id: str | None
    attempts: int
    reserved_video_s: float
    created_ago: str


@dataclass(frozen=True, slots=True)
class JobListing:
    """The result of ``render jobs`` — a filtered list from the DB mirror."""

    jobs: tuple[JobRow, ...]
    total: int
    status_filter: str | None

    def render_payload(self) -> Payload:
        data = {
            "total": self.total,
            "status_filter": self.status_filter,
            "jobs": [
                {
                    "id": j.id,
                    "priority": j.priority,
                    "status": j.status,
                    "shot_id": j.shot_id,
                    "session_id": j.session_id,
                    "attempts": j.attempts,
                    "reserved_video_s": j.reserved_video_s,
                }
                for j in self.jobs
            ],
        }
        table = Table(
            title=f"render jobs ({self.total})"
            + (f" — status={self.status_filter}" if self.status_filter else ""),
            columns=("id", "priority", "status", "shot", "attempts", "created"),
            rows=[
                (
                    j.id,
                    j.priority,
                    j.status,
                    truncate(j.shot_id, 14) if j.shot_id else "-",
                    str(j.attempts),
                    j.created_ago,
                )
                for j in self.jobs
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class JobMirror:
    """The result of ``render inspect`` — one job's mirrored DB row."""

    id: str
    priority: str
    status: str
    shot_id: str | None
    shot_hash: str | None
    session_id: str | None
    cancel_token: str | None
    attempts: int
    provider_task_id: str | None
    error: str | None
    reserved_video_s: float
    created_at_iso: str | None
    updated_at_iso: str | None

    def render_payload(self) -> Payload:
        data = {
            "id": self.id,
            "priority": self.priority,
            "status": self.status,
            "shot_id": self.shot_id,
            "shot_hash": self.shot_hash,
            "session_id": self.session_id,
            "cancel_token": self.cancel_token,
            "attempts": self.attempts,
            "provider_task_id": self.provider_task_id,
            "error": self.error,
            "reserved_video_s": self.reserved_video_s,
            "created_at": self.created_at_iso,
            "updated_at": self.updated_at_iso,
        }
        table = kv_table(f"render job (db mirror) {self.id}", dict(data))
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class DefectRow:
    id: str
    kind: str
    shot_id: str | None
    created_at_iso: str | None
    detail: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class DefectListing:
    """The result of ``render defects`` — a book's logged defects."""

    book_id: str
    defects: tuple[DefectRow, ...]

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "defects": [
                {
                    "id": d.id,
                    "kind": d.kind,
                    "shot_id": d.shot_id,
                    "created_at": d.created_at_iso,
                    "detail": d.detail,
                }
                for d in self.defects
            ],
        }
        table = Table(
            title=f"defects — book {self.book_id} ({len(self.defects)})",
            columns=("kind", "shot_id", "created", "detail"),
            rows=[
                (
                    d.kind,
                    truncate(d.shot_id, 14) if d.shot_id else "-",
                    ago(None) if d.created_at_iso is None else d.created_at_iso,
                    truncate(d.detail, 40) if d.detail else "-",
                )
                for d in self.defects
            ],
        )
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def list_jobs(
    container: Container,
    *,
    status: RenderJobStatus | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> JobListing:
    """List mirrored render jobs (newest first), optionally filtered."""
    async with container.session_factory() as db:
        stmt = select(RenderJob).order_by(RenderJob.created_at.desc())
        count_stmt = select(func.count()).select_from(RenderJob)
        if status is not None:
            stmt = stmt.where(RenderJob.status == status)
            count_stmt = count_stmt.where(RenderJob.status == status)
        if session_id is not None:
            stmt = stmt.where(RenderJob.session_id == session_id)
            count_stmt = count_stmt.where(RenderJob.session_id == session_id)
        stmt = stmt.limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        total = int((await db.execute(count_stmt)).scalar_one())
    jobs = tuple(
        JobRow(
            id=j.id,
            priority=j.priority.value,
            status=j.status.value,
            shot_id=j.shot_id,
            session_id=j.session_id,
            attempts=j.attempts,
            reserved_video_s=j.reserved_video_s,
            created_ago=ago(j.created_at),
        )
        for j in rows
    )
    return JobListing(jobs=jobs, total=total, status_filter=status.value if status else None)


async def inspect_job_mirror(container: Container, job_id: str) -> JobMirror:
    """Load one job's mirrored DB row (raises not-found if unknown)."""
    async with container.session_factory() as db:
        job = await RenderJobRepo(db).get(job_id)
        if job is None:
            raise not_found("render job", job_id)
        return JobMirror(
            id=job.id,
            priority=job.priority.value,
            status=job.status.value,
            shot_id=job.shot_id,
            shot_hash=job.shot_hash,
            session_id=job.session_id,
            cancel_token=job.cancel_token,
            attempts=job.attempts,
            provider_task_id=job.provider_task_id,
            error=job.error,
            reserved_video_s=job.reserved_video_s,
            created_at_iso=isoformat(job.created_at),
            updated_at_iso=isoformat(getattr(job, "updated_at", None)),
        )


async def list_defects(container: Container, book_id: str) -> DefectListing:
    """List a book's logged defects (newest first)."""
    from app.db.repositories.defect import DefectRepo

    async with container.session_factory() as db:
        rows = await DefectRepo(db).list_for_book(book_id)
    defects = tuple(
        DefectRow(
            id=d.id,
            kind=d.kind,
            shot_id=d.shot_id,
            created_at_iso=isoformat(d.created_at),
            detail=d.detail,
        )
        for d in rows
    )
    return DefectListing(book_id=book_id, defects=defects)


__all__ = [
    "DefectListing",
    "DefectRow",
    "JobListing",
    "JobMirror",
    "JobRow",
    "inspect_job_mirror",
    "list_defects",
    "list_jobs",
]
