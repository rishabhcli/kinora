"""``defects`` — logged shot failures (degradation-ladder drops, QA fails).

When a shot exhausts its retry cap and drops to the degradation ladder, or the
Critic rejects it, a defect is logged for later analysis and the eval harness
(kinora.md §9.5, §12.4).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class Defect(StrIdMixin, CreatedAtMixin, Base):
    """A recorded defect against a shot."""

    __tablename__ = "defects"

    shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=True
    )
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
