"""ORM models backing the durable saga store (Postgres).

Three additive tables, owned by this package and registered on ``Base.metadata``
via a single import hook in :mod:`app.db.models` (mirroring how ``app.flags`` and
``app.media`` register their own tables):

* :class:`SagaInstanceRow` (``saga_instances``) — one row per running/finished
  saga instance: its definition, correlation id (the engine-level idempotency
  key), status/outcome/cursor, the shared JSON ``state`` bag, the optional
  deadline, the worker lease, and the backoff gate. A **partial unique index** on
  ``(definition, correlation_id)`` for *active* statuses is what makes a
  re-delivered start idempotent at the database level (the same trick the jobs
  framework uses on ``job_runs.idempotency_key``).
* :class:`SagaStepRow` (``saga_steps``) — one row per step within an instance: its
  status, direction (forward/backward), forward + compensation attempt counters,
  the backoff gate, captured error, and the step's JSON ``output``. The
  ``(saga_id, step_index)`` pair is unique.
* :class:`SagaEffectRow` (``saga_effects``) — the durable effect ledger row: an
  idempotency ``key`` (unique), its applied state, the JSON ``result`` and
  ``undo_token``. This is the exactly-once backbone for the Postgres ledger.

Enums reuse the package's string enums (:mod:`app.distributed.sagas.types`,
:mod:`app.distributed.sagas.effects`) stored as portable VARCHAR + CHECK via the
shared :func:`app.db.models.enums.str_enum`, so the ORM and the in-memory/Redis
backends agree on the vocabulary.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum
from app.distributed.sagas.effects import EffectState
from app.distributed.sagas.types import (
    SagaOutcome,
    SagaStatus,
    StepDirection,
    StepStatus,
)


class SagaInstanceRow(StrIdMixin, TimestampMixin, Base):
    """Durable state for one saga instance (keyed by its opaque id)."""

    __tablename__ = "saga_instances"
    __table_args__ = (
        # Claim scan: active instances by availability + lease.
        Index("ix_saga_instances_status_available", "status", "available_at"),
        Index("ix_saga_instances_definition_created", "definition", "created_at"),
        # The non-partial query index on the correlation key; the partial UNIQUE
        # one-active-per-(definition, correlation_id) index is created in the
        # migration (SQLAlchemy can't express a partial unique cleanly cross-DB).
        Index("ix_saga_instances_correlation", "definition", "correlation_id"),
    )

    definition: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[SagaStatus] = mapped_column(
        str_enum(SagaStatus, "saga_status"),
        default=SagaStatus.PENDING,
        nullable=False,
    )
    outcome: Mapped[SagaOutcome | None] = mapped_column(
        str_enum(SagaOutcome, "saga_outcome"), nullable=True
    )
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SagaStepRow(StrIdMixin, TimestampMixin, Base):
    """One durable step record within a saga instance."""

    __tablename__ = "saga_steps"
    __table_args__ = (
        UniqueConstraint("saga_id", "step_index", name="uq_saga_steps_saga_id_step_index"),
        Index("ix_saga_steps_saga_id", "saga_id"),
    )

    saga_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        str_enum(StepStatus, "saga_step_status"),
        default=StepStatus.PENDING,
        nullable=False,
    )
    direction: Mapped[StepDirection] = mapped_column(
        str_enum(StepDirection, "saga_step_direction"),
        default=StepDirection.FORWARD,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comp_attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class SagaEffectRow(StrIdMixin, TimestampMixin, Base):
    """A durable effect-ledger record — the exactly-once backbone over Postgres."""

    __tablename__ = "saga_effects"
    __table_args__ = (
        UniqueConstraint("key", name="uq_saga_effects_key"),
        Index("ix_saga_effects_key", "key"),
    )

    key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[EffectState] = mapped_column(
        str_enum(EffectState, "saga_effect_state"),
        default=EffectState.PENDING,
        nullable=False,
    )
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undo_token: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["SagaEffectRow", "SagaInstanceRow", "SagaStepRow"]
