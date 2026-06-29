"""Saga / process-manager engine: saga_instances + saga_steps + saga_effects

Revision ID: sagas_0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29 12:00:00.000000

Additive migration for the distributed saga / process-manager engine
(``app/distributed/sagas/`` — facet C). Adds three tables that back
:class:`app.distributed.sagas.db_store.PostgresSagaStore` and
:class:`app.distributed.sagas.db_store.PostgresEffectLedger`:

* ``saga_instances`` — durable per-instance state (definition, correlation id,
  status/outcome/cursor, shared JSON state, deadline, worker lease, backoff gate);
* ``saga_steps`` — one row per step (status, direction, forward + compensation
  attempt counters, backoff gate, error, JSON output);
* ``saga_effects`` — the durable exactly-once effect ledger (idempotency key,
  applied state, JSON result + undo token).

Two database-level guarantees:

* **Idempotent start** — a **partial unique index** on
  ``saga_instances(definition, correlation_id)`` restricted to *active* statuses
  (pending/running/compensating/timed_out) so two nodes that start the same
  correlation collide on the index and only one instance survives, while finished
  history under the same correlation is retained.
* **Exactly-once effects** — a UNIQUE constraint on ``saga_effects.key`` so a
  second claim of an idempotency key collides and the action runs at most once.

Touches no existing table. The down_revision matches the shared base used by the
sibling subsystem migrations cut off the same checkout.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "sagas_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Active (non-terminal) instance statuses the partial unique index covers.
_ACTIVE_STATUSES = ("pending", "running", "compensating", "timed_out")


def upgrade() -> None:
    op.create_table(
        "saga_instances",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("definition", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'compensating', 'completed', "
            "'compensated', 'failed', 'aborted', 'timed_out')",
            name=op.f("ck_saga_instances_saga_status"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('committed', 'compensated', 'failed', 'aborted')",
            name=op.f("ck_saga_instances_saga_outcome"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saga_instances")),
    )
    op.create_index(
        "ix_saga_instances_status_available",
        "saga_instances",
        ["status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_saga_instances_definition_created",
        "saga_instances",
        ["definition", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_saga_instances_correlation",
        "saga_instances",
        ["definition", "correlation_id"],
        unique=False,
    )
    # Partial unique index: at most one ACTIVE instance per (definition,
    # correlation_id) — the database-level idempotent-start guarantee.
    active = ", ".join(f"'{s}'" for s in _ACTIVE_STATUSES)
    op.create_index(
        "uq_saga_instances_active_correlation",
        "saga_instances",
        ["definition", "correlation_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({active})"),
    )

    op.create_table(
        "saga_steps",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("saga_id", sa.String(length=64), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("comp_attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'compensating', "
            "'compensated', 'compensation_failed', 'skipped')",
            name=op.f("ck_saga_steps_saga_step_status"),
        ),
        sa.CheckConstraint(
            "direction IN ('forward', 'backward')",
            name=op.f("ck_saga_steps_saga_step_direction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saga_steps")),
        sa.UniqueConstraint(
            "saga_id", "step_index", name="uq_saga_steps_saga_id_step_index"
        ),
    )
    op.create_index("ix_saga_steps_saga_id", "saga_steps", ["saga_id"], unique=False)

    op.create_table(
        "saga_effects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("undo_token", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'applied')",
            name=op.f("ck_saga_effects_saga_effect_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saga_effects")),
        sa.UniqueConstraint("key", name="uq_saga_effects_key"),
    )
    op.create_index("ix_saga_effects_key", "saga_effects", ["key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saga_effects_key", table_name="saga_effects")
    op.drop_table("saga_effects")
    op.drop_index("ix_saga_steps_saga_id", table_name="saga_steps")
    op.drop_table("saga_steps")
    op.drop_index("uq_saga_instances_active_correlation", table_name="saga_instances")
    op.drop_index("ix_saga_instances_correlation", table_name="saga_instances")
    op.drop_index("ix_saga_instances_definition_created", table_name="saga_instances")
    op.drop_index("ix_saga_instances_status_available", table_name="saga_instances")
    op.drop_table("saga_instances")
