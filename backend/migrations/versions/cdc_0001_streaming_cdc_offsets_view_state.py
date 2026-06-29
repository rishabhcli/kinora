"""Streaming CDC data plane: cdc_offsets + cdc_view_state

Revision ID: cdc_0001
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29 00:00:00.000000

Additive migration for the change-data-capture + materialised-view subsystem
(``app/streaming/cdc/``, facet C). Adds two standalone tables — the per-connector
committed change-log offset and an optional persisted materialised-view-state
checkpoint — that are deliberately *not* foreign-keyed into the operational
schema (the CDC plane must keep resuming even as mirrored rows come and go).
Touches no existing table. Branches off the bitemporal base ``a1b2c3d4e5f6``
exactly like the sibling analytics / finops / media facets, so this is its own
head until the marathon's final merge.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "cdc_0001"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cdc_offsets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connector", sa.String(length=128), nullable=False),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("position_major", sa.BigInteger(), nullable=False),
        sa.Column("position_minor", sa.BigInteger(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cdc_offsets")),
        sa.UniqueConstraint("connector", "table_name", name="uq_cdc_offsets_connector"),
    )
    op.create_index(op.f("ix_cdc_offsets_connector"), "cdc_offsets", ["connector"], unique=False)

    op.create_table(
        "cdc_view_state",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("view_name", sa.String(length=128), nullable=False),
        sa.Column("row_key", sa.String(length=512), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cdc_view_state")),
        sa.UniqueConstraint("view_name", "row_key", name="uq_cdc_view_state_view_name"),
    )
    op.create_index(
        op.f("ix_cdc_view_state_view_name"),
        "cdc_view_state",
        ["view_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_cdc_view_state_view_name"), table_name="cdc_view_state")
    op.drop_table("cdc_view_state")
    op.drop_index(op.f("ix_cdc_offsets_connector"), table_name="cdc_offsets")
    op.drop_table("cdc_offsets")
