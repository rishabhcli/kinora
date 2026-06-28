"""Recommendations warehouse — ``book_interactions``, ``book_features``, ``user_taste_vectors``.

The server-side recsys (``app.recommendations``) folds these three tables into
its candidate-generation → scoring → re-ranking pipeline:

* ``book_interactions`` — the append-only reader↔book event log. The CF matrix,
  the popularity model, and the taste vectors are all folds over this stream.
* ``book_features`` — the per-book content feature row: the canon-centroid
  embedding (the §8 shared 1152-d space, reused so content similarity is cosine
  in the same space as the Critic / episodic retrieval), a backfilled popularity
  prior, and coarse tags/genre for the diversity + business-rule side.
* ``user_taste_vectors`` — the cached, incrementally-folded per-user taste vector
  plus the decay bookkeeping (``last_event_at`` / ``event_count``), so taste is
  carried forward rather than recomputed from the whole log every request.

All three are additive tables on the current Alembic head; they touch no existing
table beyond ``books`` / ``users`` foreign keys (both ``ON DELETE CASCADE`` for
the per-book/per-user warehouse rows).
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin

#: The shared image+text embedding dimension D (matches entities/shots, §8).
_EMBED_DIM = 1152


class BookInteraction(StrIdMixin, CreatedAtMixin, Base):
    """One reader↔book engagement event (the append-only recsys signal log)."""

    __tablename__ = "book_interactions"
    __table_args__ = (
        # Fold a user's own history (taste vector + content seeds).
        Index("ix_book_interactions_user_created", "user_id", "created_at"),
        # Fold a book's readers (item-item CF column + popularity).
        Index("ix_book_interactions_book_created", "book_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The interaction kind value (``view`` / ``finish`` / ``like`` / ``dislike`` …).
    #: Stored as a plain string so a new kind never needs a schema change; the
    #: recsys maps it back to ``app.recommendations.types.InteractionKind``.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Optional explicit feedback weight overriding the kind's implicit weight.
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Optional engagement dwell time, seconds (for dwell-scaled weighting).
    dwell_s: Mapped[float | None] = mapped_column(Float, nullable=True)


class BookFeatureRow(StrIdMixin, TimestampMixin, Base):
    """Per-book cached content feature row (one row per book, keyed by book_id)."""

    __tablename__ = "book_features"
    __table_args__ = (Index("ix_book_features_book", "book_id", unique=True),)

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The book's canon-centroid embedding (shared 1152-d space). Nullable while a
    #: freshly-imported book has no entities yet — content recall then skips it.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBED_DIM), nullable=True)
    #: Backfilled global popularity prior (cold-start + a mild ranking prior).
    popularity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Coarse categorical tags (genre/era) — JSON list of strings.
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)


class UserTasteVector(StrIdMixin, TimestampMixin, Base):
    """Cached, incrementally-folded per-user taste vector + decay bookkeeping."""

    __tablename__ = "user_taste_vectors"
    __table_args__ = (Index("ix_user_taste_vectors_user", "user_id", unique=True),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The un-normalized decayed weighted sum (the accumulator's ``sum_vec``),
    #: so the next fold can roll the decay forward without re-reading history.
    sum_vec: Mapped[list[float] | None] = mapped_column(Vector(_EMBED_DIM), nullable=True)
    #: The accumulated L1 mass (``weight_total``) — the cold-start gate.
    weight_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: The reference time the decays are referenced to (the accumulator ``as_of``).
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Number of folded events (telemetry / cold-start threshold).
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: When the cached vector was last refreshed (independent of row updated_at).
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
