"""books.user_id — durable book ownership

Revision ID: c8f1a2b3d4e5
Revises: b7d4e9a1c3f2
Create Date: 2026-06-23 04:10:00.000000

Adds a nullable ``books.user_id`` foreign key — the *durable* source of truth for
book ownership (kinora.md §5.1/§12), replacing the Redis-only ownership set in
the authz checks. Nullable with ``ON DELETE SET NULL`` so deleting a user orphans
(rather than cascade-deletes) their books; a NULL owner is accessible to nobody
(fail-closed). Additive: it touches no existing data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f1a2b3d4e5"
down_revision: str | None = "b7d4e9a1c3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("books", sa.Column("user_id", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_books_user_id"), "books", ["user_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_books_user_id_users"),
        "books",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_books_user_id_users"), "books", type_="foreignkey")
    op.drop_index(op.f("ix_books_user_id"), table_name="books")
    op.drop_column("books", "user_id")
