"""search_documents + search_index_aliases (server-side search engine, §8)

Revision ID: s1a2b3c4d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

The denormalized search-index table (kinora.md §8 — search complements the
canon, the canon stays authoritative). Each row is a flattened projection of a
library object (book / page / scene / beat / canon entity / shot):

* a *generated* weighted ``search_vector`` (``tsvector``, title=A, keywords=B,
  body=C) with a GIN index — the lexical / ``ts_rank`` arm;
* a 1152-d pgvector ``embedding`` with an HNSW cosine index — the dense arm
  (mirroring the entities/shots ANN indexes from the phase-2 migration);
* JSONB ``facets`` / ``numbers`` for facet aggregation + range filters.

The composite PK ``(index_version, doc_id)`` lets several index versions coexist
during a zero-downtime bulk reindex; ``search_index_aliases`` maps a stable alias
(``kinora_current``) to the live version so a reindex is an atomic alias swap.

Purely additive + reversible. Chains the current head ``a1b2c3d4e5f6``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "s1a2b3c4d5e6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The weighted tsvector expression — kept identical to
#: ``app.db.models.search.SEARCH_VECTOR_SQL``.
_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(keywords_text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'C')"
)


def upgrade() -> None:
    # The pgvector extension is created by the phase-2 migration; guard anyway so
    # this migration can be applied to a fresh DB created via create_all in tests.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "search_documents",
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref_id", sa.String(length=64), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("keywords_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "facets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("embedding", Vector(1152), nullable=True),
        sa.Column("boost", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("index_version", "doc_id", name=op.f("pk_search_documents")),
    )
    op.create_index(
        "ix_search_documents_version_book",
        "search_documents",
        ["index_version", "book_id"],
        unique=False,
    )
    op.create_index(
        "ix_search_documents_version_kind",
        "search_documents",
        ["index_version", "kind"],
        unique=False,
    )

    # The generated weighted tsvector column (FTS / lexical arm) + its GIN index.
    op.execute(
        "ALTER TABLE search_documents "
        f"ADD COLUMN search_vector tsvector GENERATED ALWAYS AS ({_SEARCH_VECTOR_SQL}) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_documents_search_vector "
        "ON search_documents USING gin (search_vector)"
    )

    # The dense ANN index — HNSW cosine, matching the entities/shots indexes.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_documents_embedding ON search_documents "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "search_index_aliases",
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("alias", name=op.f("pk_search_index_aliases")),
    )


def downgrade() -> None:
    op.drop_table("search_index_aliases")
    op.execute("DROP INDEX IF EXISTS ix_search_documents_embedding")
    op.execute("DROP INDEX IF EXISTS ix_search_documents_search_vector")
    op.drop_index("ix_search_documents_version_kind", table_name="search_documents")
    op.drop_index("ix_search_documents_version_book", table_name="search_documents")
    op.drop_table("search_documents")
