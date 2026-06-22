"""``sessions`` — per-reader scheduler/reading state (kinora.md §4.9).

Holds the live reading-position model the Scheduler needs: focus word ``w``,
reading velocity, committed-seconds-ahead of the buffer, the in-flight job sets,
remaining budget, and the last-activity timestamp used for idle-pause.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import SessionMode, str_enum


class Session(StrIdMixin, TimestampMixin, Base):
    """A live reading session and its scheduler state."""

    __tablename__ = "sessions"

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    focus_word: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    velocity_wps: Mapped[float] = mapped_column(Float, default=4.0, nullable=False)
    committed_seconds_ahead: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mode: Mapped[SessionMode] = mapped_column(
        str_enum(SessionMode, "session_mode"),
        default=SessionMode.VIEWER,
        nullable=False,
    )
    # {"committed": [shot_id, ...], "speculative": [shot_id, ...]}
    inflight: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    budget_remaining_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_activity_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
