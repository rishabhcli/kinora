"""DB-backed tuple store + decision-log sink for the authorization plane.

These bind the plane's in-memory protocols to the ``authz_*`` tables so the
relation graph and the audit log persist across processes. The design keeps the
*pure* :class:`~app.platform.authz.rebac.RelationGraph` unchanged: the DB store
implements the exact same :class:`~app.platform.authz.rebac.TupleStore` protocol
(three lookups + ``object_types``), so the graph's check + reverse-index logic is
identical whether it runs over the in-memory or the SQL store.

Two access patterns are provided:

* :class:`DbTupleStore` — a *synchronous-protocol* store that the relation graph
  calls. Because SQLAlchemy's async session cannot be awaited from the graph's
  synchronous recursion, the store eager-loads a **consistent snapshot** of the
  tuple set into an in-memory store at construction (``await DbTupleStore.load``).
  This is the standard Zanzibar approach: a check runs against a point-in-time
  snapshot. Writes go to both the snapshot and the database.
* :class:`DbDecisionLog` — an append-only sink that batches decision rows and
  flushes them to ``authz_decision_log`` (fire-and-forget audit; never on the
  hot path of the decision itself).

This module imports SQLAlchemy and the DB session factory, so it is the one
I/O-bound corner of the plane; everything else stays pure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.authz.audit import DecisionRecord
from app.platform.authz.db_models import AuthzDecisionLogRow, AuthzRelationTuple
from app.platform.authz.model import Decision
from app.platform.authz.rebac import (
    InMemoryTupleStore,
    ObjectRef,
    RelationTuple,
    SubjectRef,
)

SessionFactory = Callable[[], AsyncSession]


def _to_tuple(row: AuthzRelationTuple) -> RelationTuple:
    return RelationTuple(
        object=ObjectRef(type=row.object_type, id=row.object_id),
        relation=row.relation,
        subject=SubjectRef(
            type=row.subject_type,
            id=row.subject_id,
            relation=row.subject_relation,
        ),
    )


class DbTupleStore:
    """A snapshot-backed :class:`~app.platform.authz.rebac.TupleStore`.

    Construct via :meth:`load` (it eager-loads the current tuple set into an
    in-memory snapshot the graph reads synchronously). :meth:`write` /
    :meth:`delete` persist to the DB *and* update the snapshot so subsequent
    checks in the same request see the write.
    """

    def __init__(self, snapshot: InMemoryTupleStore, session: AsyncSession) -> None:
        self._snapshot = snapshot
        self._session = session

    @classmethod
    async def load(cls, session: AsyncSession) -> DbTupleStore:
        """Eager-load every persisted tuple into a fresh in-memory snapshot."""
        snapshot = InMemoryTupleStore()
        rows = (await session.execute(select(AuthzRelationTuple))).scalars().all()
        for row in rows:
            snapshot.write(_to_tuple(row))
        return cls(snapshot, session)

    # -- TupleStore protocol (synchronous, served from the snapshot) --------- #

    def subjects(self, object_: ObjectRef, relation: str) -> Iterable[SubjectRef]:
        return self._snapshot.subjects(object_, relation)

    def objects_for_subject(
        self, subject: SubjectRef, relation: str, object_type: str
    ) -> Iterable[ObjectRef]:
        return self._snapshot.objects_for_subject(subject, relation, object_type)

    def relations_pointing_at(
        self, object_: ObjectRef
    ) -> Iterable[tuple[ObjectRef, str]]:
        return self._snapshot.relations_pointing_at(object_)

    def object_types(self) -> Iterable[str]:
        return self._snapshot.object_types()

    def __len__(self) -> int:
        return len(self._snapshot)

    # -- mutation (writes through to the DB + the snapshot) ------------------ #

    async def awrite(self, t: RelationTuple) -> None:
        """Persist a tuple (idempotent) and update the snapshot."""
        self._snapshot.write(t)
        exists = await self._session.execute(
            select(AuthzRelationTuple.id).where(
                AuthzRelationTuple.object_type == t.object.type,
                AuthzRelationTuple.object_id == t.object.id,
                AuthzRelationTuple.relation == t.relation,
                AuthzRelationTuple.subject_type == t.subject.type,
                AuthzRelationTuple.subject_id == t.subject.id,
                AuthzRelationTuple.subject_relation == t.subject.relation,
            )
        )
        if exists.first() is not None:
            return
        self._session.add(
            AuthzRelationTuple(
                object_type=t.object.type,
                object_id=t.object.id,
                relation=t.relation,
                subject_type=t.subject.type,
                subject_id=t.subject.id,
                subject_relation=t.subject.relation,
            )
        )

    async def adelete(self, t: RelationTuple) -> None:
        """Remove a tuple (idempotent) from the DB and the snapshot."""
        self._snapshot.delete(t)
        await self._session.execute(
            delete(AuthzRelationTuple).where(
                AuthzRelationTuple.object_type == t.object.type,
                AuthzRelationTuple.object_id == t.object.id,
                AuthzRelationTuple.relation == t.relation,
                AuthzRelationTuple.subject_type == t.subject.type,
                AuthzRelationTuple.subject_id == t.subject.id,
                AuthzRelationTuple.subject_relation == t.subject.relation,
            )
        )


class DbDecisionLog:
    """An append-only decision-log sink that buffers then flushes to the DB.

    :meth:`record` (the sync sink protocol) only buffers — it never does I/O on
    the decision hot path. :meth:`flush` writes the buffered rows in one batch.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory
        self._buffer: list[DecisionRecord] = []

    def record(self, decision: Decision) -> None:
        self._buffer.append(DecisionRecord.from_decision(decision))

    async def flush(self) -> int:
        """Persist + clear the buffer; return the number of rows written."""
        if not self._buffer:
            return 0
        records, self._buffer = self._buffer, []
        async with self._factory() as session:
            for rec in records:
                session.add(_to_row(rec))
            await session.commit()
        return len(records)

    @property
    def pending(self) -> int:
        return len(self._buffer)


def _to_row(rec: DecisionRecord) -> AuthzDecisionLogRow:
    return AuthzDecisionLogRow(
        subject_ref=rec.subject_ref,
        action=rec.action,
        resource_ref=rec.resource_ref,
        effect=rec.effect.value,
        reasons="\n".join(rec.reasons),
        cached=rec.cached,
        digest=rec.digest,
        evaluated_at=rec.evaluated_at,
    )


async def load_persisted_records(
    session: AsyncSession, *, limit: int = 1000
) -> list[DecisionRecord]:
    """Read recent decision-log rows back as :class:`DecisionRecord`\\s (for admin)."""
    rows = (
        (
            await session.execute(
                select(AuthzDecisionLogRow)
                .order_by(AuthzDecisionLogRow.evaluated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    from app.platform.authz.model import Effect

    return [
        DecisionRecord(
            subject_ref=row.subject_ref,
            action=row.action,
            resource_ref=row.resource_ref,
            effect=Effect(row.effect),
            reasons=tuple(row.reasons.split("\n")) if row.reasons else (),
            cached=row.cached,
            evaluated_at=row.evaluated_at,
        )
        for row in rows
    ]


__all__ = ["DbDecisionLog", "DbTupleStore", "load_persisted_records"]
