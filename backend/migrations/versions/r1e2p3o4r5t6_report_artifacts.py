"""report_artifacts — index over generated report documents (reports subsystem).

Adds the ``report_artifacts`` table that indexes every rendered, stored report
(reader-facing keepsakes + operator dashboards). The row never holds the bytes —
only the object-store ``storage_key``, a ``content_hash`` for dedup, and the
metadata needed for signed retrieval, listing, and retention.

Purely additive: a single new table on the current head; no existing table is
altered, so it composes with the other agents' migrations.

Revision ID: r1e2p3o4r5t6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 17:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "r1e2p3o4r5t6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
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
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("subject_kind", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=True),
        sa.Column("params", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_report_artifacts_owner", "report_artifacts", ["user_id", "kind"]
    )
    op.create_index("ix_report_artifacts_book", "report_artifacts", ["book_id"])
    op.create_index(
        "ix_report_artifacts_dedup", "report_artifacts", ["content_hash", "format"]
    )
    op.create_index(
        "ix_report_artifacts_subject",
        "report_artifacts",
        ["subject_kind", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_artifacts_subject", table_name="report_artifacts")
    op.drop_index("ix_report_artifacts_dedup", table_name="report_artifacts")
    op.drop_index("ix_report_artifacts_book", table_name="report_artifacts")
    op.drop_index("ix_report_artifacts_owner", table_name="report_artifacts")
    op.drop_table("report_artifacts")
