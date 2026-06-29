"""Event-sourcing read-model projections (facet C)

Revision ID: esproj_0001
Revises: f3a91c7d20e4
Create Date: 2026-06-29 10:30:00.000000

Additive migration for the CQRS **read side** (``app/eventsourcing/projections/``).
Adds three ``esproj_*`` tables — the materialised read-model rows, the projection
checkpoint/health ledger (with the blue-green active-slot pointer), and the
at-least-once idempotency ledger of applied events. None are foreign-keyed to the
event log or to ``books``/``users``: read models are derived, rebuildable state
that must survive source deletions and be truncatable independently. Touches no
existing table.

Down-revision chains from the product-analytics head so this lands as a single
new leaf; the repo intentionally carries multiple Alembic heads (one per
parallel facet) and a later merge migration can converge them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "esproj_0001"
down_revision: str | None = "f3a91c7d20e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "esproj_read_models",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_esproj_read_models")),
        sa.UniqueConstraint("namespace", "key", name="uq_esproj_read_models_ns_key"),
    )
    op.create_index(
        "ix_esproj_read_models_namespace",
        "esproj_read_models",
        ["namespace"],
        unique=False,
    )

    op.create_table(
        "esproj_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("projection", sa.String(length=160), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("observed_head", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("active_slot", sa.String(length=8), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_esproj_checkpoints")),
        sa.UniqueConstraint("projection", name="uq_esproj_checkpoints_projection"),
    )

    op.create_table(
        "esproj_applied_events",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_esproj_applied_events")),
        sa.UniqueConstraint(
            "projection", "event_id", name="uq_esproj_applied_proj_event"
        ),
    )
    op.create_index(
        "ix_esproj_applied_events_projection",
        "esproj_applied_events",
        ["projection"],
        unique=False,
    )
    # Supports pruning the ledger below a committed checkpoint position.
    op.create_index(
        "ix_esproj_applied_events_proj_position",
        "esproj_applied_events",
        ["projection", "position"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_esproj_applied_events_proj_position", table_name="esproj_applied_events"
    )
    op.drop_index(
        "ix_esproj_applied_events_projection", table_name="esproj_applied_events"
    )
    op.drop_table("esproj_applied_events")

    op.drop_table("esproj_checkpoints")

    op.drop_index("ix_esproj_read_models_namespace", table_name="esproj_read_models")
    op.drop_table("esproj_read_models")
