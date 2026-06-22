"""``continuity_states`` — versioned facts with beat-interval validity.

A continuity state is a ``(subject, predicate, object)`` triple that is true only
over a beat interval. "Forgetting" (kinora.md §8.5) is implemented by *closing*
the interval (`retire_state` sets ``valid_to_beat``) rather than deleting the
row, so the fact survives for backward/time-travel reads but drops out of the
active set used for forward generation. Beats are integer ordinals (see
:mod:`app.db.models.entity`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class ContinuityState(StrIdMixin, CreatedAtMixin, Base):
    """A versioned fact scoped to the beat interval over which it holds."""

    __tablename__ = "continuity_states"
    __table_args__ = (
        Index("ix_continuity_states_subject", "book_id", "subject_entity_key"),
        Index("ix_continuity_states_book_from", "book_id", "valid_from_beat"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subject_entity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    object_value: Mapped[str] = mapped_column(Text, nullable=False)

    valid_from_beat: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_to_beat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # {"page", "word_range": [start, end]} | {"page", "char_range": [...]}
    source_span: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
