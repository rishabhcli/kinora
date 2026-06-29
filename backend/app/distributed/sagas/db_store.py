"""Durable, queryable saga store + effect ledger over Postgres.

Backs the engine with the ``saga_instances`` + ``saga_steps`` tables (and
``saga_effects`` for the exactly-once ledger), giving an *auditable* run history
alongside the crash-resume guarantees. The two race-prone paths are made safe by
the database itself, exactly as :class:`app.jobs.db_store.PostgresJobStore` does
it for jobs:

* **Idempotent start.** A partial unique index on ``(definition, correlation_id)``
  for *active* statuses means a second start for the same correlation raises an
  ``IntegrityError``; we catch it and return the existing active instance — the
  same dedup the in-memory store gives.
* **Exclusive claim.** ``claim_due`` flips the earliest runnable, unleased row to a
  fresh lease under ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers
  never drive the same saga (Postgres' row-lock is the lease's teeth).

Each public method runs in its own short unit of work via an injected committing
session factory (the same shape as :func:`app.db.session.get_session`), so the
store composes with the rest of the backend without holding a session open across
step execution. The :class:`PostgresEffectLedger` shares that factory.

Naming note: the *value types* are :class:`app.distributed.sagas.types.SagaInstance`
/ ``StepRecord``; the *ORM rows* are :class:`app.distributed.sagas.models.*` —
imported here with a ``Row`` suffix.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.distributed.sagas.effects import EffectRecord, EffectState, _ensure_jsonable, _OnceMixin
from app.distributed.sagas.models import (
    SagaEffectRow,
    SagaInstanceRow,
    SagaStepRow,
)
from app.distributed.sagas.store import LoadedSaga, SagaStats, StartResult
from app.distributed.sagas.types import (
    SagaInstance,
    SagaOutcome,
    SagaStatus,
    StepDirection,
    StepRecord,
    StepStatus,
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_ACTIVE = (
    SagaStatus.PENDING,
    SagaStatus.RUNNING,
    SagaStatus.COMPENSATING,
    SagaStatus.TIMED_OUT,
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _inst_row_to_value(row: SagaInstanceRow) -> SagaInstance:
    return SagaInstance(
        id=row.id,
        definition=row.definition,
        correlation_id=row.correlation_id,
        status=row.status,
        cursor=row.cursor,
        created_at=_aware(row.created_at),
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at),
        outcome=row.outcome,
        error=row.error,
        deadline=_aware(row.deadline),
        state=dict(row.state or {}),
        available_at=_aware(row.available_at),
        lease_token=row.lease_token,
        lease_until=_aware(row.lease_until),
    )


def _step_row_to_value(row: SagaStepRow) -> StepRecord:
    return StepRecord(
        saga_id=row.saga_id,
        index=row.step_index,
        name=row.name,
        status=row.status,
        direction=row.direction,
        attempt=row.attempt,
        comp_attempt=row.comp_attempt,
        max_attempts=row.max_attempts,
        available_at=_aware(row.available_at),
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at),
        error=row.error,
        output=dict(row.output or {}),
    )


def _apply_instance(row: SagaInstanceRow, value: SagaInstance) -> None:
    row.status = value.status
    row.outcome = value.outcome
    row.cursor = value.cursor
    row.state = dict(value.state)
    row.error = value.error[:4000] if value.error else None
    row.started_at = value.started_at
    row.finished_at = value.finished_at
    row.deadline = value.deadline
    row.available_at = value.available_at
    row.lease_token = value.lease_token
    row.lease_until = value.lease_until


def _apply_step(row: SagaStepRow, value: StepRecord) -> None:
    row.status = value.status
    row.direction = value.direction
    row.attempt = value.attempt
    row.comp_attempt = value.comp_attempt
    row.max_attempts = value.max_attempts
    row.available_at = value.available_at
    row.started_at = value.started_at
    row.finished_at = value.finished_at
    row.error = value.error[:4000] if value.error else None
    row.output = dict(value.output)


class PostgresSagaStore:
    """A durable :class:`SagaStore` over the ``saga_instances`` + ``saga_steps`` tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def start(
        self,
        *,
        definition: str,
        correlation_id: str,
        steps: list[StepRecord],
        state: dict[str, Any] | None = None,
        deadline: datetime | None = None,
    ) -> StartResult:
        existing = await self._active_for_key(definition, correlation_id)
        if existing is not None:
            return StartResult(instance=existing, created=False)
        saga_id = uuid.uuid4().hex
        inst_row = SagaInstanceRow(
            id=saga_id,
            definition=definition,
            correlation_id=correlation_id,
            status=SagaStatus.PENDING,
            cursor=0,
            state=dict(state or {}),
            deadline=deadline,
        )
        try:
            async with self._sf() as db:
                db.add(inst_row)
                for s in steps:
                    db.add(
                        SagaStepRow(
                            id=uuid.uuid4().hex,
                            saga_id=saga_id,
                            step_index=s.index,
                            name=s.name,
                            status=StepStatus.PENDING,
                            direction=StepDirection.FORWARD,
                            max_attempts=s.max_attempts,
                            output={},
                        )
                    )
                await db.flush()
                created = _inst_row_to_value(inst_row)
            return StartResult(instance=created, created=True)
        except IntegrityError:
            current = await self._active_for_key(definition, correlation_id)
            if current is not None:
                return StartResult(instance=current, created=False)
            raise

    async def _active_for_key(self, definition: str, correlation_id: str) -> SagaInstance | None:
        async with self._sf() as db:
            stmt = (
                select(SagaInstanceRow)
                .where(
                    SagaInstanceRow.definition == definition,
                    SagaInstanceRow.correlation_id == correlation_id,
                    SagaInstanceRow.status.in_(_ACTIVE),
                )
                .limit(1)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            return _inst_row_to_value(row) if row is not None else None

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        definitions: list[str] | None = None,
    ) -> SagaInstance | None:
        token = uuid.uuid4().hex
        async with self._sf() as db:
            conditions = [
                SagaInstanceRow.status.in_(_ACTIVE),
                or_(
                    SagaInstanceRow.available_at.is_(None),
                    SagaInstanceRow.available_at <= now,
                ),
                or_(
                    SagaInstanceRow.lease_until.is_(None),
                    SagaInstanceRow.lease_until <= now,
                ),
            ]
            if definitions is not None:
                conditions.append(SagaInstanceRow.definition.in_(definitions))
            stmt = (
                select(SagaInstanceRow)
                .where(and_(*conditions))
                .order_by(
                    SagaInstanceRow.available_at.asc().nulls_first(),
                    SagaInstanceRow.created_at.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.lease_token = token
            row.lease_until = now + timedelta(seconds=lease_seconds)
            await db.flush()
            return _inst_row_to_value(row)

    async def load(self, saga_id: str) -> LoadedSaga | None:
        async with self._sf() as db:
            inst = await db.get(SagaInstanceRow, saga_id)
            if inst is None:
                return None
            stmt = (
                select(SagaStepRow)
                .where(SagaStepRow.saga_id == saga_id)
                .order_by(SagaStepRow.step_index.asc())
            )
            step_rows = (await db.execute(stmt)).scalars().all()
            return LoadedSaga(
                instance=_inst_row_to_value(inst),
                steps=[_step_row_to_value(r) for r in step_rows],
            )

    async def save_instance(self, instance: SagaInstance) -> None:
        async with self._sf() as db:
            row = await db.get(SagaInstanceRow, instance.id)
            if row is None:
                return
            _apply_instance(row, instance)
            await db.flush()

    async def save_step(self, step: StepRecord) -> None:
        async with self._sf() as db:
            stmt = select(SagaStepRow).where(
                SagaStepRow.saga_id == step.saga_id,
                SagaStepRow.step_index == step.index,
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return
            _apply_step(row, step)
            await db.flush()

    async def get(self, saga_id: str) -> SagaInstance | None:
        async with self._sf() as db:
            row = await db.get(SagaInstanceRow, saga_id)
            return _inst_row_to_value(row) if row is not None else None

    async def list_instances(
        self,
        *,
        definition: str | None = None,
        status: SagaStatus | None = None,
        limit: int = 100,
    ) -> list[SagaInstance]:
        async with self._sf() as db:
            stmt = select(SagaInstanceRow)
            if definition is not None:
                stmt = stmt.where(SagaInstanceRow.definition == definition)
            if status is not None:
                stmt = stmt.where(SagaInstanceRow.status == status)
            stmt = stmt.order_by(SagaInstanceRow.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [_inst_row_to_value(r) for r in rows]

    async def reap_expired(self, *, now: datetime) -> int:
        async with self._sf() as db:
            stmt = (
                select(SagaInstanceRow)
                .where(
                    SagaInstanceRow.status.in_(_ACTIVE),
                    SagaInstanceRow.lease_until.is_not(None),
                    SagaInstanceRow.lease_until <= now,
                )
                .with_for_update(skip_locked=True)
            )
            rows = list((await db.execute(stmt)).scalars().all())
            for row in rows:
                row.lease_token = None
                row.lease_until = None
                row.available_at = now
            await db.flush()
            return len(rows)

    async def stats(self) -> SagaStats:
        async with self._sf() as db:
            stmt = select(SagaInstanceRow.status, func.count()).group_by(SagaInstanceRow.status)
            rows = (await db.execute(stmt)).all()
            by_status = {status.value: int(count) for status, count in rows}
            return SagaStats(
                by_status=by_status,
                started_total=sum(by_status.values()),
                committed_total=by_status.get(SagaStatus.COMPLETED.value, 0),
                compensated_total=by_status.get(SagaStatus.COMPENSATED.value, 0),
                failed_total=by_status.get(SagaStatus.FAILED.value, 0),
            )


class PostgresEffectLedger(_OnceMixin):
    """A durable, cross-process :class:`EffectLedger` over the ``saga_effects`` table.

    The claim is an ``INSERT`` of a PENDING row that collides on the unique ``key``
    index for a second claimer (caught as ``IntegrityError`` → claim lost). The
    applied result is recorded by flipping that row to APPLIED with the JSON
    result/undo, so a replay reads it back instead of re-running the action.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> EffectRecord | None:
        async with self._sf() as db:
            stmt = select(SagaEffectRow).where(SagaEffectRow.key == key).limit(1)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return EffectRecord(
                key=row.key,
                state=row.state,
                result=row.result,
                undo_token=row.undo_token,
                created_at=_aware(row.created_at),
                applied_at=_aware(row.applied_at),
            )

    async def claim(self, key: str) -> bool:
        try:
            async with self._sf() as db:
                db.add(
                    SagaEffectRow(
                        id=uuid.uuid4().hex,
                        key=key,
                        state=EffectState.PENDING,
                    )
                )
                await db.flush()
            return True
        except IntegrityError:
            return False

    async def record(self, key: str, *, result: Any, undo_token: Any = None) -> None:
        result = _ensure_jsonable(result)
        undo_token = _ensure_jsonable(undo_token)
        async with self._sf() as db:
            stmt = select(SagaEffectRow).where(SagaEffectRow.key == key).limit(1)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = SagaEffectRow(id=uuid.uuid4().hex, key=key)
                db.add(row)
            row.state = EffectState.APPLIED
            row.result = result
            row.undo_token = undo_token
            row.applied_at = datetime.now(UTC)
            await db.flush()

    async def forget(self, key: str) -> None:
        async with self._sf() as db:
            stmt = select(SagaEffectRow).where(SagaEffectRow.key == key)
            for row in (await db.execute(stmt)).scalars().all():
                await db.delete(row)
            await db.flush()


# Re-export the outcome enum for callers reading the value types from the store.
__all__ = ["PostgresEffectLedger", "PostgresSagaStore", "SagaOutcome"]
