"""A database-backed :class:`~app.audit.store.AuditSink` over an AsyncSession.

This is the production sink: the same :class:`~app.audit.service.AuditService`
that runs against :class:`~app.audit.store.InMemoryAuditSink` in tests runs
unchanged here. It translates between the storage-neutral
:class:`~app.audit.store.AuditRecord` / :class:`~app.audit.store.CheckpointRecord`
dataclasses and the ORM rows in :mod:`app.audit.db_models`.

The sink *flushes* but does not *commit* — the unit-of-work boundary owns the
transaction, exactly like every other repository. It exposes :meth:`rollback`
so the service can recover from a lost ``seq`` race (the unique ``(seq)``
constraint surfaces as ``IntegrityError`` on flush) and retry against the new
tail.

This module is intentionally import-light at the package level (it pulls in
SQLAlchemy + the ORM) so the pure core (taxonomy / chain / redaction / events /
service / in-memory store) stays usable with no DB on the path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.db_models import AuditCheckpoint, AuditLogEntry
from app.audit.query import AuditQuery
from app.audit.store import AuditRecord, CheckpointRecord


class DbAuditSink:
    """An :class:`~app.audit.store.AuditSink` backed by a SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def rollback(self) -> None:
        """Roll back the session after a lost (seq) race so the retry is clean."""
        await self.session.rollback()

    async def tail(self) -> AuditRecord | None:
        stmt = select(AuditLogEntry).order_by(AuditLogEntry.seq.desc()).limit(1)
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def append(self, record: AuditRecord) -> AuditRecord:
        row = AuditLogEntry(
            id=record.id,
            seq=record.seq,
            occurred_at=record.occurred_at,
            category=record.category,
            action=record.action,
            severity=record.severity,
            actor_kind=record.actor_kind,
            actor_id=record.actor_id,
            target_type=record.target_type,
            target_id=record.target_id,
            correlation_id=record.correlation_id,
            trace_id=record.trace_id,
            reason=record.reason,
            before=record.before,
            after=record.after,
            payload=record.payload,
            prev_hash=record.prev_hash,
            entry_hash=record.entry_hash,
            sealed=record.sealed,
        )
        self.session.add(row)
        await self.session.flush()  # surfaces the unique (seq) constraint as IntegrityError
        return _to_record(row)

    async def all_ordered(self) -> list[AuditRecord]:
        stmt = select(AuditLogEntry).order_by(AuditLogEntry.seq.asc())
        return [_to_record(r) for r in (await self.session.execute(stmt)).scalars().all()]

    async def query(self, query: AuditQuery) -> list[AuditRecord]:
        stmt = _apply_filters(select(AuditLogEntry), query)
        stmt = stmt.order_by(
            AuditLogEntry.seq.asc() if query.ascending else AuditLogEntry.seq.desc()
        )
        if query.offset:
            stmt = stmt.offset(query.offset)
        if query.limit is not None:
            stmt = stmt.limit(query.limit)
        return [_to_record(r) for r in (await self.session.execute(stmt)).scalars().all()]

    async def count(self, query: AuditQuery) -> int:
        stmt = _apply_filters(select(func.count()).select_from(AuditLogEntry), query)
        return int((await self.session.execute(stmt)).scalar_one())

    async def latest_checkpoint(self) -> CheckpointRecord | None:
        stmt = select(AuditCheckpoint).order_by(AuditCheckpoint.seq.desc()).limit(1)
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_checkpoint(row) if row is not None else None

    async def append_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        row = AuditCheckpoint(
            id=checkpoint.id,
            seq=checkpoint.seq,
            from_seq=checkpoint.from_seq,
            to_seq=checkpoint.to_seq,
            merkle_root=checkpoint.merkle_root,
            prev_checkpoint_hash=checkpoint.prev_checkpoint_hash,
            checkpoint_hash=checkpoint.checkpoint_hash,
        )
        self.session.add(row)
        await self.session.flush()
        return _to_checkpoint(row)

    async def all_checkpoints(self) -> list[CheckpointRecord]:
        stmt = select(AuditCheckpoint).order_by(AuditCheckpoint.seq.asc())
        return [_to_checkpoint(r) for r in (await self.session.execute(stmt)).scalars().all()]

    async def mark_sealed(self, up_to_seq: int) -> int:
        stmt = select(AuditLogEntry).where(
            AuditLogEntry.seq <= up_to_seq, AuditLogEntry.sealed.is_(False)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            row.sealed = True
        await self.session.flush()
        return len(rows)

    async def redact_payload(
        self,
        seq: int,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        payload: dict[str, Any] | None,
        reason: str | None,
    ) -> bool:
        stmt = select(AuditLogEntry).where(AuditLogEntry.seq == seq)
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            return False
        row.before = before
        row.after = after
        row.payload = payload
        row.reason = reason
        await self.session.flush()
        return True

    async def prune_before(self, before_seq: int) -> int:
        stmt = select(AuditLogEntry).where(
            AuditLogEntry.seq < before_seq, AuditLogEntry.sealed.is_(True)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        return len(rows)


def _apply_filters(stmt: Any, query: AuditQuery) -> Any:
    if query.actor_kinds:
        stmt = stmt.where(AuditLogEntry.actor_kind.in_(query.actor_kinds))
    if query.actor_ids:
        stmt = stmt.where(AuditLogEntry.actor_id.in_(query.actor_ids))
    if query.categories:
        stmt = stmt.where(AuditLogEntry.category.in_(query.categories))
    if query.actions:
        stmt = stmt.where(AuditLogEntry.action.in_(query.actions))
    if query.severities:
        stmt = stmt.where(AuditLogEntry.severity.in_(query.severities))
    if query.target_type is not None:
        stmt = stmt.where(AuditLogEntry.target_type == query.target_type)
    if query.target_ids:
        stmt = stmt.where(AuditLogEntry.target_id.in_(query.target_ids))
    if query.correlation_id is not None:
        stmt = stmt.where(AuditLogEntry.correlation_id == query.correlation_id)
    if query.trace_id is not None:
        stmt = stmt.where(AuditLogEntry.trace_id == query.trace_id)
    if query.since is not None:
        stmt = stmt.where(AuditLogEntry.occurred_at >= query.since)
    if query.until is not None:
        stmt = stmt.where(AuditLogEntry.occurred_at < query.until)
    return stmt


def _to_record(row: AuditLogEntry) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        seq=row.seq,
        occurred_at=row.occurred_at,
        category=row.category,
        action=row.action,
        severity=row.severity,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        target_type=row.target_type,
        target_id=row.target_id,
        correlation_id=row.correlation_id,
        trace_id=row.trace_id,
        reason=row.reason,
        before=row.before,
        after=row.after,
        payload=row.payload,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        created_at=row.created_at,
        sealed=row.sealed,
    )


def _to_checkpoint(row: AuditCheckpoint) -> CheckpointRecord:
    return CheckpointRecord(
        id=row.id,
        seq=row.seq,
        from_seq=row.from_seq,
        to_seq=row.to_seq,
        merkle_root=row.merkle_root,
        prev_checkpoint_hash=row.prev_checkpoint_hash,
        checkpoint_hash=row.checkpoint_hash,
        created_at=row.created_at,
    )


__all__ = ["DbAuditSink"]
