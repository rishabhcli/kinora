"""``search_documents`` — the denormalized search index table (kinora.md §8).

A read-optimised projection of every searchable library object (book / page /
scene / beat / canon entity / shot) flattened into the fields the Postgres
search backend ranks on:

* a generated ``tsvector`` (``search_vector``) over the weighted text fields,
  with a GIN index — the lexical (BM25-ish ``ts_rank``) arm;
* a 1152-d pgvector ``embedding`` — the dense semantic arm (cosine ``<=>``);
* JSONB ``facets`` + ``numbers`` for facet aggregation + range filters.

This is **not** authoritative (the canon is, §8): the row is keyed by
``(index_version, doc_id)`` so a re-index can rebuild it from the canon at any
time, and the ``index_version`` column lets several index versions coexist
during a zero-downtime bulk reindex (the alias points at the live version).
"""

from __future__ import annotations

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Float,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

#: The weighted ``tsvector`` expression (title=A, keywords=B, body=C). Postgres
#: ``ts_rank`` weights A>B>C, so a title hit outranks a body hit — mirroring the
#: in-memory backend's :data:`app.search.documents.FIELD_BOOSTS`. Kept as a
#: module constant so the migration and the ORM use the exact same SQL.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(keywords_text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(body, '')), 'C')"
)


class SearchDocumentRow(TimestampMixin, Base):
    """One indexed search document (a flattened projection of a canon object)."""

    __tablename__ = "search_documents"
    __table_args__ = (
        # The hot lookup: scope a search to a book within an index version.
        Index("ix_search_documents_version_book", "index_version", "book_id"),
        Index("ix_search_documents_version_kind", "index_version", "kind"),
        # The pgvector ANN index + the FTS GIN index are created in the migration
        # (``USING gin`` / ``USING ivfflat`` aren't expressible via Index() here).
    )

    # Composite PK ``(index_version, doc_id)`` so versions coexist during reindex.
    index_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(160), primary_key=True)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Keywords flattened to a single text field so the generated tsvector can
    #: weight them (B). The structured list lives in ``payload`` if needed.
    keywords_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    facets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    numbers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(1152), nullable=True)

    # A relevance/recency tiebreaker so facet-only queries have a stable order.
    boost: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class SearchIndexAlias(TimestampMixin, Base):
    """Maps a stable alias name (``kinora_current``) to a live ``index_version``.

    The versioned-alias swap (zero-downtime bulk reindex): a full reindex builds
    a fresh ``index_version`` then atomically repoints the alias row at it; reads
    resolve the alias to the version. Old versions can then be dropped.
    """

    __tablename__ = "search_index_aliases"

    alias: Mapped[str] = mapped_column(String(64), primary_key=True)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = ["SEARCH_VECTOR_SQL", "SearchDocumentRow", "SearchIndexAlias"]
