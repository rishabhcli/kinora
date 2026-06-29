"""Durable-workflow engine: executions + events + tasks + activity tasks + timers

Revision ID: workflows_0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29 12:00:00.000000

Additive migration for the Temporal-style durable-execution engine
(``app/platform/workflows/`` — distinct from the jobs framework and the shot
render queue). Adds the five tables that back
:class:`app.platform.workflows.db_store.PostgresWorkflowStore`:

* ``workflow_executions`` — one row per run (status, input, result/error,
  ``last_event_id`` for optimistic-concurrency appends, parent links);
* ``workflow_events`` — the append-only event history. ``(workflow_id, run_id,
  event_id)`` is UNIQUE — that constraint is the database-level guarantee behind
  the optimistic-concurrency append (a racing second appender collides on it);
* ``workflow_tasks`` — leased workflow-task queue (visibility-timeout claim);
* ``workflow_activity_tasks`` — durable activity executions (leased, heartbeated,
  retried);
* ``workflow_timers`` — durable timers promoted to ``TIMER_FIRED``.

Branches off ``a1b2c3d4e5f6`` (the shared base the parallel platform packages
fork from), in its own head so it merges cleanly. Touches no existing table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "workflows_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXECUTION_STATUSES = (
    "running",
    "completed",
    "failed",
    "cancelled",
    "continued_as_new",
    "timed_out",
)


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_type", sa.String(length=255), nullable=False),
        sa.Column("task_queue", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("input_args", sa.JSON(), nullable=False),
        sa.Column("input_kwargs", sa.JSON(), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("parent_workflow_id", sa.String(length=255), nullable=True),
        sa.Column("parent_run_id", sa.String(length=64), nullable=True),
        sa.Column("parent_seq", sa.Integer(), nullable=True),
        sa.Column("memo", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _EXECUTION_STATUSES) + ")",
            name=op.f("ck_workflow_executions_workflow_execution_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_executions")),
        sa.UniqueConstraint(
            "workflow_id", "run_id", name="uq_workflow_executions_workflow_id_run_id"
        ),
    )
    op.create_index(
        "ix_workflow_executions_workflow_id", "workflow_executions", ["workflow_id"], unique=False
    )
    op.create_index(
        "ix_workflow_executions_status", "workflow_executions", ["status"], unique=False
    )
    op.create_index(
        "ix_workflow_executions_parent",
        "workflow_executions",
        ["parent_workflow_id", "parent_run_id"],
        unique=False,
    )

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_events")),
        sa.UniqueConstraint(
            "workflow_id", "run_id", "event_id", name="uq_workflow_events_run_event_id"
        ),
    )
    op.create_index(
        "ix_workflow_events_run",
        "workflow_events",
        ["workflow_id", "run_id", "event_id"],
        unique=False,
    )

    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_tasks")),
    )
    op.create_index(
        "ix_workflow_tasks_visible", "workflow_tasks", ["visible_at", "lease_until"], unique=False
    )
    op.create_index(
        "ix_workflow_tasks_run", "workflow_tasks", ["workflow_id", "run_id"], unique=False
    )

    op.create_table(
        "workflow_activity_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("activity_type", sa.String(length=255), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("kwargs", sa.JSON(), nullable=False),
        sa.Column("task_queue", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=True),
        sa.Column("start_to_close_timeout_s", sa.Float(), nullable=True),
        sa.Column("schedule_to_close_timeout_s", sa.Float(), nullable=True),
        sa.Column("heartbeat_timeout_s", sa.Float(), nullable=True),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_activity_tasks")),
    )
    op.create_index(
        "ix_workflow_activity_tasks_visible",
        "workflow_activity_tasks",
        ["task_queue", "visible_at", "lease_until"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_activity_tasks_run",
        "workflow_activity_tasks",
        ["workflow_id", "run_id", "seq"],
        unique=False,
    )

    op.create_table(
        "workflow_timers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_timers")),
    )
    op.create_index(
        "ix_workflow_timers_fire", "workflow_timers", ["fire_at", "cancelled"], unique=False
    )
    op.create_index(
        "ix_workflow_timers_run", "workflow_timers", ["workflow_id", "run_id", "seq"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_timers_run", table_name="workflow_timers")
    op.drop_index("ix_workflow_timers_fire", table_name="workflow_timers")
    op.drop_table("workflow_timers")

    op.drop_index("ix_workflow_activity_tasks_run", table_name="workflow_activity_tasks")
    op.drop_index("ix_workflow_activity_tasks_visible", table_name="workflow_activity_tasks")
    op.drop_table("workflow_activity_tasks")

    op.drop_index("ix_workflow_tasks_run", table_name="workflow_tasks")
    op.drop_index("ix_workflow_tasks_visible", table_name="workflow_tasks")
    op.drop_table("workflow_tasks")

    op.drop_index("ix_workflow_events_run", table_name="workflow_events")
    op.drop_table("workflow_events")

    op.drop_index("ix_workflow_executions_parent", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_status", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_workflow_id", table_name="workflow_executions")
    op.drop_table("workflow_executions")
