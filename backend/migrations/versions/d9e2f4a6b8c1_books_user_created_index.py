"""books(user_id, created_at) covering index for the durable library shelf

Revision ID: d9e2f4a6b8c1
Revises: c8f1a2b3d4e5
Create Date: 2026-06-26

The shelf list (``BookRepo.list_for_user``) filters ``WHERE user_id = ?`` and orders by
``created_at DESC``. This composite ``(user_id, created_at)`` lets Postgres serve a *bounded*
shelf read — ``... ORDER BY created_at DESC LIMIT k`` — as an ordered Index Scan Backward with
**no filesort**. Measured on this migration's schema (2000 books for one user, ANALYZE'd):

    LIMIT 20 :  Index Scan Backward using ix_books_user_created, no Sort  → 0.013 ms
    no LIMIT :  Bitmap Index Scan on ix_books_user_id + quicksort         → 0.317 ms

So the win (~24×) is realized once the shelf query **paginates**; today's un-paginated
``list_for_user`` returns the whole shelf and the planner picks bitmap+sort either way, leaving
this index unused for that exact statement. It is shipped as the *enabler* for the paginated /
preloaded 100-book library, paired with a pagination proposal to the BookRepo owner (Agent 5) in
``coordination/requests/agent-07.md`` (R6). Purely additive + reversible; book inserts are rare.

``ix_books_user_id`` is left in place (FK + ``count_for_user``); a later cleanup could drop it once
this composite is the canonical user-prefix index — flagged for Agent 12 (it belongs to the
ownership migration), not dropped here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d9e2f4a6b8c1"
down_revision: str | None = "c8f1a2b3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_books_user_created",
        "books",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_books_user_created", table_name="books")
