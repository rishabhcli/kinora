"""Database-backed integration tests for the audit subsystem's DB sink.

Gated on ``KINORA_TEST_DATABASE_URL`` so they skip cleanly in the default
(infra-free) suite, exactly like the rest of the DB-bound tests. They confirm
the :class:`~app.audit.db.DbAuditSink` is a faithful :class:`AuditSink`: the same
:class:`AuditService` that passes against the in-memory sink in
``test_audit_unit`` records, verifies, queries, reconstructs provenance, seals,
and survives a real lost-``seq`` race here against Postgres.

The fixture creates *only* the two audit tables (``audit_log_entries`` /
``audit_checkpoints``) on a throwaway connection and truncates them per-test, so
these never touch the wider schema.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.audit import registry  # noqa: F401  (registers the audit tables)
from app.audit.db import DbAuditSink
from app.audit.db_models import AuditCheckpoint, AuditLogEntry
from app.audit.query import AuditQuery
from app.audit.service import AuditService
from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
)
from app.db.base import Base

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="audit DB tests require KINORA_TEST_DATABASE_URL"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # Create only the audit tables; truncate them for isolation.
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[AuditLogEntry.__table__, AuditCheckpoint.__table__],
            )
            await conn.execute(
                text('TRUNCATE "audit_log_entries", "audit_checkpoints" RESTART IDENTITY CASCADE')
            )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as sess:
            yield sess
            await sess.rollback()
    finally:
        await engine.dispose()


async def test_db_sink_records_verifies_and_queries(session: AsyncSession) -> None:
    svc = AuditService(DbAuditSink(session), segment_size=2)
    corr = "render_db_1"
    await svc.record_event(
        AuditAction.CANON_UPDATED,
        actor_kind=AuditActorKind.AGENT,
        actor_id="continuity",
        target_type="canon_fact",
        target_id="char_1",
        correlation_id=corr,
        payload={"email": "alice@example.com", "ok": True},
    )
    await svc.record_event(
        AuditAction.RENDER_ACCEPTED,
        actor_kind=AuditActorKind.SYSTEM,
        actor_id="render-worker",
        target_type="clip",
        target_id="shot_7",
        correlation_id=corr,
    )
    await session.commit()

    report = await svc.verify_integrity()
    assert report.ok and report.chain.entries == 2
    # One full segment of 2 was auto-sealed.
    assert report.checkpoints_verified == 1

    # PII redacted on the way into Postgres.
    (canon,) = await svc.query(AuditQuery(categories=frozenset({AuditCategory.CANON})))
    assert canon.payload is not None
    assert "__redacted__" in canon.payload["email"]
    assert canon.payload["ok"] is True

    # Provenance trail spans the correlation.
    trail = await svc.provenance_trail("shot_7", target_type="clip")
    assert {e.action for e in trail.events} == {
        AuditAction.CANON_UPDATED,
        AuditAction.RENDER_ACCEPTED,
    }


async def test_db_sink_unique_seq_blocks_a_duplicate(session: AsyncSession) -> None:
    """The unique (seq) constraint is what serialises concurrent appenders."""
    from sqlalchemy.exc import IntegrityError

    sink = DbAuditSink(session)
    svc = AuditService(sink, segment_size=1000)
    await svc.record_event(
        AuditAction.AUTH_LOGIN, actor_kind=AuditActorKind.USER, actor_id="usr_1"
    )
    await session.commit()

    # Force a duplicate seq=1 directly; the DB must reject it.
    dup = AuditLogEntry(
        id="dup",
        seq=1,
        occurred_at=datetime.now(UTC),
        category=AuditCategory.AUTH,
        action=AuditAction.AUTH_LOGIN,
        severity=AuditSeverity.INFO,
        actor_kind=AuditActorKind.USER,
        actor_id="attacker",
        target_type=None,
        target_id=None,
        correlation_id=None,
        trace_id=None,
        reason=None,
        before=None,
        after=None,
        payload=None,
        prev_hash="00" * 32,
        entry_hash="ff" * 32,
        sealed=False,
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
