"""``prefs`` — director-preference signals, persistent across sessions (§8.6).

Every Director edit writes/nudges a preference (a "slower" note bumps a pacing
prior, repeated palette edits shift the default Style). Preferences accumulate
per user and/or per book and are read into the Cinematographer's prompt prior on
the next session.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin


class Pref(StrIdMixin, TimestampMixin, Base):
    """A single accumulated preference signal."""

    __tablename__ = "prefs"
    __table_args__ = (Index("ix_prefs_scope_kind", "user_id", "book_id", "kind"),)

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
