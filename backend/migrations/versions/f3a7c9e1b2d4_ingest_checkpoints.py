"""ingest_checkpoints — durable Phase-A milestone ledger (Agent: ingest, §9.1).

Adds the ``ingest_checkpoints`` table: one row per completed Phase-A milestone
(extract / analyse / canon / shot_plan / identity_lock) per book, so a crashed
or restarted ingest can resume from where it left off instead of recomputing
finished stages. Purely additive — no existing table is touched; the milestone
column is a portable VARCHAR + CHECK (matching the ``str_enum`` convention).

Revision ID: f3a7c9e1b2d4
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a7c9e1b2d4"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MILESTONES = ("extract", "analyze", "canon", "shot_plan", "identity_lock")


def upgrade() -> None:
    op.create_table(
        "ingest_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column(
            "milestone",
            sa.Enum(
                *_MILESTONES,
                name="ingest_milestone",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_ingest_checkpoints_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_checkpoints")),
        sa.UniqueConstraint("book_id", "milestone", name="uq_ingest_checkpoint"),
    )
    op.create_index(
        op.f("ix_ingest_checkpoints_book_id"),
        "ingest_checkpoints",
        ["book_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ingest_checkpoints_book_id"), table_name="ingest_checkpoints")
    op.drop_table("ingest_checkpoints")
