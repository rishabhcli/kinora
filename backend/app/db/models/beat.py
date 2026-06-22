"""``beats`` — the smallest planning atom (a sentence-or-two of intent, §4.2).

One beat usually maps to one shot. ``beat_index`` is the **monotonic integer
ordinal** of the beat within its book and is the bridge between the string
``beat_id`` the agents pass around (``beat_0034``) and the integer beat used for
beat-interval validity reads (``Entity.valid_from_beat`` /
``ContinuityState.valid_to_beat``): ``canon.query`` looks a beat up here, reads
its ``beat_index``, and resolves canon *as of* that ordinal (§8.4).

``entities`` is the list of canon ``entity_key`` s the Adapter detected as
present in the beat; ``canon.query`` resolves each *at this beat's version* —
this is exactly what keeps a 300-page book's per-shot context to a few hundred
tokens of *relevant* canon instead of the whole book.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class Beat(StrIdMixin, CreatedAtMixin, Base):
    """A single narrative beat and the canon entities present in it."""

    __tablename__ = "beats"
    __table_args__ = (
        # The beat ordinal is unique within a book (it is the as-of-beat key).
        UniqueConstraint("book_id", "beat_index"),
        Index("ix_beats_book_scene_index", "book_id", "scene_id", "beat_index"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scenes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    beat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # ["char_elsa", "loc_window", ...] — canon entity_keys present in this beat.
    entities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    described_visuals: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(Text, nullable=True)

    # {"page", "para", "word_range": [start, end]}
    source_span: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
