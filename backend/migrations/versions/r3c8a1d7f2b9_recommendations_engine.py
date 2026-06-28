"""Recommendations engine: book_interactions, book_features, user_taste_vectors

Revision ID: r3c8a1d7f2b9
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28 12:00:00.000000

Additive migration for the server-side recommendations engine
(``app.recommendations``). It stacks on the current head and touches no existing
table beyond ``books`` / ``users`` foreign keys:

* ``book_interactions`` — the append-only reader↔book event log the CF matrix,
  popularity model, and taste vectors are folded from.
* ``book_features`` — per-book cached content features (the §8 1152-d canon
  centroid embedding, popularity prior, tags).
* ``user_taste_vectors`` — the cached, incrementally-folded per-user taste vector
  + decay bookkeeping.

The two ``Vector(1152)`` columns require the pgvector extension, already created
by the initial Phase-2 migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "r3c8a1d7f2b9"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBED_DIM = 1152


def upgrade() -> None:
    op.create_table(
        "book_interactions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("dwell_s", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_book_interactions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_book_interactions_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_interactions")),
    )
    op.create_index(
        op.f("ix_book_interactions_user_id"), "book_interactions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_book_interactions_book_id"), "book_interactions", ["book_id"], unique=False
    )
    op.create_index(
        "ix_book_interactions_user_created",
        "book_interactions",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_book_interactions_book_created",
        "book_interactions",
        ["book_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "book_features",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(_EMBED_DIM), nullable=True),
        sa.Column("popularity", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_book_features_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_features")),
    )
    op.create_index(op.f("ix_book_features_book_id"), "book_features", ["book_id"], unique=False)
    op.create_index("ix_book_features_book", "book_features", ["book_id"], unique=True)

    op.create_table(
        "user_taste_vectors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("sum_vec", Vector(_EMBED_DIM), nullable=True),
        sa.Column("weight_total", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_taste_vectors_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_taste_vectors")),
    )
    op.create_index(
        op.f("ix_user_taste_vectors_user_id"), "user_taste_vectors", ["user_id"], unique=False
    )
    op.create_index("ix_user_taste_vectors_user", "user_taste_vectors", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_taste_vectors_user", table_name="user_taste_vectors")
    op.drop_index(op.f("ix_user_taste_vectors_user_id"), table_name="user_taste_vectors")
    op.drop_table("user_taste_vectors")

    op.drop_index("ix_book_features_book", table_name="book_features")
    op.drop_index(op.f("ix_book_features_book_id"), table_name="book_features")
    op.drop_table("book_features")

    op.drop_index("ix_book_interactions_book_created", table_name="book_interactions")
    op.drop_index("ix_book_interactions_user_created", table_name="book_interactions")
    op.drop_index(op.f("ix_book_interactions_book_id"), table_name="book_interactions")
    op.drop_index(op.f("ix_book_interactions_user_id"), table_name="book_interactions")
    op.drop_table("book_interactions")
