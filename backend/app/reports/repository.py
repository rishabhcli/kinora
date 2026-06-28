"""Repository over :class:`~app.reports.db_model.ReportArtifact`.

A thin read/write repository following the project convention (flush, never
commit — the unit of work owns the transaction). Holds the artifact index
queries: create a row, find a deduped row by content hash, list a reader's or an
operator's artifacts, and find expired rows for the retention sweep.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.reports.db_model import (
    ReportArtifact,
    ReportAudience,
    ReportFormatEnum,
    ReportKind,
    ReportStatus,
)


class ReportArtifactRepo(BaseRepository):
    """Create + query report-artifact index rows."""

    async def create(
        self,
        *,
        kind: ReportKind,
        audience: ReportAudience,
        fmt: ReportFormatEnum,
        title: str,
        user_id: str | None = None,
        book_id: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        storage_key: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        status: ReportStatus = ReportStatus.READY,
        trigger: str | None = None,
        params: dict | None = None,
        expires_at: datetime | None = None,
        error: str | None = None,
    ) -> ReportArtifact:
        """Insert a new artifact index row."""
        row = ReportArtifact(
            kind=kind,
            audience=audience,
            format=fmt,
            title=title,
            user_id=user_id,
            book_id=book_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            storage_key=storage_key,
            content_hash=content_hash,
            size_bytes=size_bytes,
            status=status,
            trigger=trigger,
            params=params,
            expires_at=expires_at,
            error=error,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, artifact_id: str) -> ReportArtifact | None:
        """Fetch one artifact by id."""
        return await self.session.get(ReportArtifact, artifact_id)

    async def find_dedup(
        self, *, content_hash: str, fmt: ReportFormatEnum
    ) -> ReportArtifact | None:
        """Find a ready artifact with the same content hash + format (dedup)."""
        stmt = (
            select(ReportArtifact)
            .where(
                ReportArtifact.content_hash == content_hash,
                ReportArtifact.format == fmt,
                ReportArtifact.status == ReportStatus.READY,
            )
            .order_by(ReportArtifact.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, kind: ReportKind | None = None
    ) -> list[ReportArtifact]:
        """A reader's artifacts, newest first (optionally filtered by kind)."""
        stmt = select(ReportArtifact).where(ReportArtifact.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(ReportArtifact.kind == kind)
        stmt = stmt.order_by(ReportArtifact.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_audience(
        self, audience: ReportAudience, *, limit: int = 50
    ) -> list[ReportArtifact]:
        """All artifacts for an audience, newest first (operator console)."""
        stmt = (
            select(ReportArtifact)
            .where(ReportArtifact.audience == audience)
            .order_by(ReportArtifact.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[ReportArtifact]:
        """Artifacts whose ``expires_at`` has passed (retention sweep)."""
        stmt = (
            select(ReportArtifact)
            .where(
                ReportArtifact.expires_at.is_not(None),
                ReportArtifact.expires_at < now,
            )
            .order_by(ReportArtifact.expires_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete(self, artifact_id: str) -> None:
        """Delete one artifact index row."""
        row = await self.get(artifact_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()


__all__ = ["ReportArtifactRepo"]
