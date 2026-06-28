"""Jobs framework: scheduled_jobs + job_runs

Revision ID: c0ffeejobs01
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 17:00:00.000000

Additive migration for the general background-jobs framework (``app/jobs/`` —
distinct from the shot render queue). Adds two tables that back
:class:`app.jobs.db_store.PostgresJobStore`:

* ``scheduled_jobs`` — durable schedule state per registered job (name, trigger,
  enabled/paused, last-fire bookmark);
* ``job_runs`` — one row per execution with the retry/backoff gate, worker lease,
  captured error, and structured detail.

The at-least-once dedup guarantee is enforced at the DB level by a **partial
unique index** on ``job_runs.idempotency_key`` restricted to *active* statuses
(pending/running/retrying) — so two nodes that enqueue the same due instant
collide on the index and only one row survives, while completed/failed history
under the same key is retained. Touches no existing table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0ffeejobs01"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The active (non-terminal) run statuses the partial unique index covers.
_ACTIVE_STATUSES = ("pending", "running", "retrying")


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("trigger_spec", sa.String(length=256), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('cron', 'interval', 'once', 'manual')",
            name=op.f("ck_scheduled_jobs_job_trigger_kind"),
        ),
        sa.CheckConstraint(
            "state IN ('enabled', 'paused')",
            name=op.f("ck_scheduled_jobs_scheduled_job_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_jobs")),
        sa.UniqueConstraint("name", name=op.f("uq_scheduled_jobs_name")),
    )
    op.create_index(
        op.f("ix_scheduled_jobs_name"), "scheduled_jobs", ["name"], unique=False
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'skipped', 'retrying', "
            "'failed', 'deadletter', 'cancelled')",
            name=op.f("ck_job_runs_job_run_status"),
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('cron', 'interval', 'once', 'manual')",
            name=op.f("ck_job_runs_job_run_trigger_kind"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('success', 'skipped', 'failed')",
            name=op.f("ck_job_runs_job_run_outcome"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    op.create_index(
        "ix_job_runs_status_available", "job_runs", ["status", "available_at"], unique=False
    )
    op.create_index(
        "ix_job_runs_job_name_created", "job_runs", ["job_name", "created_at"], unique=False
    )
    op.create_index(
        "ix_job_runs_idempotency_key", "job_runs", ["idempotency_key"], unique=False
    )
    # Partial unique index: at most one ACTIVE run per idempotency key (the
    # database-level guarantee behind at-least-once + idempotent enqueue).
    active = ", ".join(f"'{s}'" for s in _ACTIVE_STATUSES)
    op.create_index(
        "uq_job_runs_active_idempotency_key",
        "job_runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({active})"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_runs_active_idempotency_key", table_name="job_runs")
    op.drop_index("ix_job_runs_idempotency_key", table_name="job_runs")
    op.drop_index("ix_job_runs_job_name_created", table_name="job_runs")
    op.drop_index("ix_job_runs_status_available", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_index(op.f("ix_scheduled_jobs_name"), table_name="scheduled_jobs")
    op.drop_table("scheduled_jobs")
