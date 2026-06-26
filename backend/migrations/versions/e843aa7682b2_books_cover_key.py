"""books cover_key — a real, dedicated shelf cover (Agent 05, §5.1).

Adds ``books.cover_key`` (nullable object-store key of the book's cover image,
``covers/{book_id}``), distinct from "page 1's image". Projected as a presigned
``BookResponse.cover_url`` and served by ``GET /books/{id}/cover``.

Purely additive: existing rows get ``NULL`` (no cover yet → generated fallback).

Revision ID: e843aa7682b2
Revises: c8f1a2b3d4e5
Create Date: 2026-06-26 17:27:45.601965

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e843aa7682b2"
down_revision: str | None = "c8f1a2b3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("books", sa.Column("cover_key", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("books", "cover_key")
