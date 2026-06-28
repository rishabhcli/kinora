"""FinOps: append-only USD cost_ledger

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 09:30:00.000000

Additive migration (kinora.md §11.1 / §12.5). Adds the append-only ``cost_ledger``
the FinOps layer (``app.finops``) writes USD-valued spend into — distinct from the
``budget_ledger`` which meters the hard-capped video-seconds. It touches no
existing table; the cost ledger is a pure addition for attribution + reconciliation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COST_KINDS = ("chat", "vl", "image", "tts", "asr", "embedding", "video", "other")


def upgrade() -> None:
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("scene_id", sa.String(length=64), nullable=True),
        sa.Column("shot_id", sa.String(length=64), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(*_COST_KINDS, name="cost_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("images", sa.Integer(), nullable=False),
        sa.Column("audio_seconds", sa.Float(), nullable=False),
        sa.Column("video_seconds", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_ledger")),
    )
    op.create_index(
        "ix_cost_ledger_scope",
        "cost_ledger",
        ["tenant_id", "book_id", "session_id"],
        unique=False,
    )
    op.create_index("ix_cost_ledger_shot", "cost_ledger", ["shot_id"], unique=False)
    op.create_index("ix_cost_ledger_kind", "cost_ledger", ["kind"], unique=False)
    op.create_index("ix_cost_ledger_agent", "cost_ledger", ["agent"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cost_ledger_agent", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_kind", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_shot", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_scope", table_name="cost_ledger")
    op.drop_table("cost_ledger")
