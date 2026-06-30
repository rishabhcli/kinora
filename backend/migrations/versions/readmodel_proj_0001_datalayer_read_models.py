"""Datalayer read-model projections (facet C, productionised)

Revision ID: readmodel_proj_0001
Revises: ace1a7010c30
Create Date: 2026-06-29 22:30:00.000000

Additive migration for the consolidated read side (``app/datalayer/``). Adds three
``datalayer_*`` tables — the materialised read-model rows, the projection
checkpoint/health ledger, and the at-least-once idempotency ledger of applied
events. None are foreign-keyed to the event log or to ``books`` / ``users``: read
models are derived, rebuildable state that must survive source deletions and be
truncatable independently. Touches no existing table.

These are deliberately distinct from the older ``esproj_*`` tables: that read side
consumes an adapter event contract, whereas this one reads ``RecordedEvent``
straight from the real ``app.eventsourcing.store`` event store.

Down-revision chains from the current single head ``ace1a7010c30`` so the tree
stays single-headed after this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "readmodel_proj_0001"
down_revision: str | None = "ace1a7010c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datalayer_read_models",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=160), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datalayer_read_models")),
        sa.UniqueConstraint("namespace", "key", name="uq_datalayer_read_models_ns_key"),
    )
    op.create_index(
        "ix_datalayer_read_models_namespace",
        "datalayer_read_models",
        ["namespace"],
        unique=False,
    )

    op.create_table(
        "datalayer_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("projection", sa.String(length=160), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("observed_head", sa.BigInteger(), nullable=False),
        sa.Column("events_applied", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("projection_version", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datalayer_checkpoints")),
        sa.UniqueConstraint("projection", name="uq_datalayer_checkpoints_projection"),
    )

    op.create_table(
        "datalayer_applied_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("projection", sa.String(length=160), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datalayer_applied_events")),
        sa.UniqueConstraint(
            "projection", "event_id", name="uq_datalayer_applied_proj_event"
        ),
    )
    op.create_index(
        "ix_datalayer_applied_events_projection",
        "datalayer_applied_events",
        ["projection"],
        unique=False,
    )
    # Supports pruning the ledger below a committed checkpoint position.
    op.create_index(
        "ix_datalayer_applied_events_proj_position",
        "datalayer_applied_events",
        ["projection", "position"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_datalayer_applied_events_proj_position",
        table_name="datalayer_applied_events",
    )
    op.drop_index(
        "ix_datalayer_applied_events_projection",
        table_name="datalayer_applied_events",
    )
    op.drop_table("datalayer_applied_events")

    op.drop_table("datalayer_checkpoints")

    op.drop_index(
        "ix_datalayer_read_models_namespace", table_name="datalayer_read_models"
    )
    op.drop_table("datalayer_read_models")
