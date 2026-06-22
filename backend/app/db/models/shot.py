"""Shot aggregate: ``shots`` (episodic store), ``source_span_index``, ``shot_cache``.

* ``shots`` is the episodic / vector store (kinora.md §8.2): every shot ever
  generated, its prompt/seed/references, output keys, narration, QA scores, and
  a 1152-d embedding for "what worked before" retrieval.
* ``source_span_index`` is the §4.2 sorted map ``word_index → shot``. The btree
  on ``(book_id, word_index_start)`` is what resolves a scroll position to a
  shot in O(log n).
* ``shot_cache`` is the §8.7 content-hash cache: a hit serves the cached clip
  from object storage and spends zero video-seconds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import ShotStatus, str_enum


class Shot(StrIdMixin, TimestampMixin, Base):
    """One generated clip and its full episodic record."""

    __tablename__ = "shots"
    __table_args__ = (
        Index("ix_shots_book_status", "book_id", "status"),
        Index("ix_shots_book_beat", "book_id", "beat_id"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    beat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # {"page", "para", "word_range": [start, end]}
    source_span: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[ShotStatus] = mapped_column(
        str_enum(ShotStatus, "shot_status"),
        default=ShotStatus.PLANNED,
        nullable=False,
        index=True,
    )
    render_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    reference_set_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # ["char_elsa_001@v3", "loc_window@v1", ...]
    reference_image_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # {"clip_key", "clip_url", "last_frame_key"}
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {"text", "audio_key", "word_timestamps": [...]}
    narration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {"ccs", "style_drift", "timeline_ok", "motion_artifact", "score", "verdict", "reason"}
    qa: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {"video_seconds", "tokens"}
    cost: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(1152), nullable=True)
    canon_version_at_render: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shot_hash: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceSpanIndex(StrIdMixin, CreatedAtMixin, Base):
    """Sorted ``word_index → shot`` map; resolves scroll position in O(log n)."""

    __tablename__ = "source_span_index"
    __table_args__ = (
        # The §4.2 lookup: greatest ``word_index_start`` <= focus word.
        Index("ix_source_span_index_book_word", "book_id", "word_index_start"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    word_index_start: Mapped[int] = mapped_column(Integer, nullable=False)
    word_index_end: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    beat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ShotCache(CreatedAtMixin, Base):
    """Content-hash keyed cache of accepted shots (kinora.md §8.7)."""

    __tablename__ = "shot_cache"

    shot_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    clip_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_frame_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # {"word_timestamps", "page_turn_at_s", ...}
    sync_segment: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    qa: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    video_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
