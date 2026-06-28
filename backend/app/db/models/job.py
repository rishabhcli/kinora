"""``scheduled_jobs`` + ``job_runs`` — durable state for the jobs framework.

These back :class:`app.jobs.db_store.PostgresJobStore`, giving the general
background-jobs framework (``app/jobs/``, distinct from the shot render queue)
a queryable audit trail and a crash-durable run store:

* :class:`ScheduledJob` — one row per registered, scheduled job; its enabled/paused
  state and a denormalised ``last_fire_at`` the leader uses to avoid re-deriving
  fire times. (The registry is the source of truth for the *handler*; this row is
  the durable *schedule state*.)
* :class:`JobRun` — one row per execution (an attempt-set under a stable
  ``idempotency_key``); the lifecycle, retry/backoff gate (``available_at``),
  worker lease, captured error, and structured detail. A partial unique index on
  ``idempotency_key`` for *active* runs is what makes at-least-once enqueue
  idempotent at the database level.

The enums reuse the framework's :class:`app.jobs.types` string enums so the ORM
and the in-memory/Redis stores agree on the vocabulary.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum
from app.jobs.types import JobRunStatus, RunOutcome, ScheduledJobState, TriggerKind


class ScheduledJob(StrIdMixin, TimestampMixin, Base):
    """Durable schedule state for one registered job (keyed by its unique name)."""

    __tablename__ = "scheduled_jobs"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    trigger_kind: Mapped[TriggerKind] = mapped_column(
        str_enum(TriggerKind, "job_trigger_kind"), nullable=False
    )
    trigger_spec: Mapped[str | None] = mapped_column(String(256), nullable=True)
    state: Mapped[ScheduledJobState] = mapped_column(
        str_enum(ScheduledJobState, "scheduled_job_state"),
        default=ScheduledJobState.ENABLED,
        nullable=False,
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobRun(StrIdMixin, TimestampMixin, Base):
    """One durable execution record (attempt-set) for a scheduled/ad-hoc job."""

    __tablename__ = "job_runs"
    __table_args__ = (
        # History/claim scan: due, non-terminal runs by availability.
        Index("ix_job_runs_status_available", "status", "available_at"),
        Index("ix_job_runs_job_name_created", "job_name", "created_at"),
        # Partial unique index enforcing one ACTIVE run per idempotency key is
        # created in the migration (SQLAlchemy can't express a partial unique
        # cleanly across backends); this Index is the non-partial query index.
        Index("ix_job_runs_idempotency_key", "idempotency_key"),
    )

    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[JobRunStatus] = mapped_column(
        str_enum(JobRunStatus, "job_run_status"),
        default=JobRunStatus.PENDING,
        nullable=False,
    )
    trigger_kind: Mapped[TriggerKind] = mapped_column(
        str_enum(TriggerKind, "job_run_trigger_kind"),
        default=TriggerKind.MANUAL,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[RunOutcome | None] = mapped_column(
        str_enum(RunOutcome, "job_run_outcome"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["JobRun", "ScheduledJob"]
