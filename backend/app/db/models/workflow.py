"""Durable-execution engine tables — the persistence behind the workflow engine.

These ORM rows back :class:`app.platform.workflows.db_store.PostgresWorkflowStore`,
the crash-durable backend for the Temporal-style engine in
:mod:`app.platform.workflows` (distinct from the operational jobs framework's
``scheduled_jobs``/``job_runs`` and the shot render queue). Five tables:

* :class:`WorkflowExecutionRow` — one row per execution (a run id). Holds the
  workflow type/input, the lifecycle status, the result/error, ``last_event_id``
  for optimistic-concurrency appends, and the parent-link columns that make child
  workflows resolvable back to their parent.
* :class:`WorkflowEventRow` — the append-only **event history**, one row per
  :class:`~app.platform.workflows.events.HistoryEvent`. ``(workflow_id, run_id,
  event_id)`` is unique — that uniqueness is the database-level guarantee behind
  the optimistic-concurrency append (a racing second appender collides).
* :class:`WorkflowTaskRow` — the "this run has new events, give it a task"
  queue, leased with a visibility timeout (at-least-once workflow-task delivery).
* :class:`WorkflowActivityTaskRow` — durable activity executions, leased +
  heartbeated + retried, keyed per ``(run, seq)``.
* :class:`WorkflowTimerRow` — durable timers the timer service promotes to
  ``TIMER_FIRED``.

The enums reuse the engine's string enums so the ORM and the in-memory/Redis
stores agree on the vocabulary.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum
from app.platform.workflows.store import ExecutionStatus


class WorkflowExecutionRow(StrIdMixin, TimestampMixin, Base):
    """One durable workflow execution (keyed by ``id`` = the engine run id)."""

    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "run_id", name="uq_workflow_executions_workflow_id_run_id"),
        Index("ix_workflow_executions_workflow_id", "workflow_id"),
        Index("ix_workflow_executions_status", "status"),
        Index("ix_workflow_executions_parent", "parent_workflow_id", "parent_run_id"),
    )

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(255), nullable=False)
    task_queue: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    status: Mapped[ExecutionStatus] = mapped_column(
        str_enum(ExecutionStatus, "workflow_execution_status"),
        default=ExecutionStatus.RUNNING,
        nullable=False,
    )
    input_args: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    input_kwargs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_event_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parent_workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memo: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class WorkflowEventRow(StrIdMixin, Base):
    """One append-only history event for a run (``event_id`` gap-free, 1-based)."""

    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "run_id", "event_id", name="uq_workflow_events_run_event_id"
        ),
        Index("ix_workflow_events_run", "workflow_id", "run_id", "event_id"),
    )

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class WorkflowTaskRow(StrIdMixin, Base):
    """A pending workflow task (this run has unprocessed events). Leased."""

    __tablename__ = "workflow_tasks"
    __table_args__ = (
        Index("ix_workflow_tasks_visible", "visible_at", "lease_until"),
        Index("ix_workflow_tasks_run", "workflow_id", "run_id"),
    )

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WorkflowActivityTaskRow(StrIdMixin, Base):
    """A durable activity execution (at-least-once, leased, heartbeated, retried)."""

    __tablename__ = "workflow_activity_tasks"
    __table_args__ = (
        Index("ix_workflow_activity_tasks_visible", "task_queue", "visible_at", "lease_until"),
        Index("ix_workflow_activity_tasks_run", "workflow_id", "run_id", "seq"),
    )

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(255), nullable=False)
    args: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    kwargs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    task_queue: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    start_to_close_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    schedule_to_close_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeat_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WorkflowTimerRow(StrIdMixin, Base):
    """A durable timer that fires ``TIMER_FIRED`` at ``fire_at``."""

    __tablename__ = "workflow_timers"
    __table_args__ = (
        Index("ix_workflow_timers_fire", "fire_at", "cancelled"),
        Index("ix_workflow_timers_run", "workflow_id", "run_id", "seq"),
    )

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


__all__ = [
    "WorkflowActivityTaskRow",
    "WorkflowEventRow",
    "WorkflowExecutionRow",
    "WorkflowTaskRow",
    "WorkflowTimerRow",
]
