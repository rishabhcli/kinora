"""``entities`` — versioned canon nodes (character / location / prop / style).

Each row is one *version* of a logical entity (identified by ``entity_key``,
e.g. ``char_elsa``). Versions carry a beat-interval validity
(``valid_from_beat`` .. ``valid_to_beat``) so the canon can be read *as of* any
beat (time-travel reads) and superseded versions drop out of forward retrieval
(kinora.md §8.1). ``valid_from_beat`` / ``valid_to_beat`` are monotonic integer
beat ordinals within the book so interval containment is a plain numeric
comparison (and therefore index-friendly).
"""

from __future__ import annotations

from typing import Any

# pgvector provides the SQLAlchemy ``Vector`` column type (the 1152-d shared
# image+text embedding from DashScope ``tongyi-embedding-vision-plus``, used by
# the Critic's similarity check and episodic retrieval).
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_id
from app.db.models.enums import EntityType, str_enum


class Entity(TimestampMixin, Base):
    """A single version of a canon entity."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("book_id", "entity_key", "version"),
        Index("ix_entities_book_key", "book_id", "entity_key"),
        Index("ix_entities_book_type", "book_id", "type"),
        # Covers the time-travel read filter (book_id, entity_key, validity interval),
        # so get_as_of_beat / get_present_as_of_beat don't scan all versions of a key.
        Index(
            "ix_entities_key_valid",
            "book_id",
            "entity_key",
            "valid_from_beat",
            "valid_to_beat",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    entity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[EntityType] = mapped_column(str_enum(EntityType, "entity_type"), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)

    aliases: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # {"description", "reference_image_keys": [...], "locked": bool, ...}
    appearance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {"cosyvoice_voice_id", "reference_audio_key", "params": {...}}
    voice: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Style nodes only: {"palette", "lens", "art_direction"}
    style_tokens: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(1152), nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    valid_from_beat: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_to_beat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supersedes: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )

    # {"page", "beat_id"}
    first_appearance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
