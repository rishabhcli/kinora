"""``budget_ledger`` — the append-only ledger backing the budget service (§11.1).

Video-seconds are the scarce, hard-capped currency. Rather than mutate a single
counter (which races and loses history), every budget movement is an immutable
row:

* ``reserve`` — earmark seconds before a render (``reservation_id`` == the row's
  own id, so the reservation is identified by this row);
* ``commit``  — a render finished; charge the *actual* seconds against the
  reservation (``reservation_id`` points back at the ``reserve`` row);
* ``release`` — a render was cancelled/cache-hit; return the earmarked seconds
  (``reservation_id`` points back at the ``reserve`` row).

A reservation is **outstanding** while no ``commit``/``release`` row references
it. ``remaining = ceiling − Σ committed − Σ outstanding_reserved``. This makes
the per-session and per-scene caps a windowed sum over the same ledger.
"""

from __future__ import annotations

import enum

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin
from app.db.models.enums import str_enum


class BudgetKind(enum.StrEnum):
    """The three movements an entry can record."""

    RESERVE = "reserve"
    COMMIT = "commit"
    RELEASE = "release"


class BudgetLedger(StrIdMixin, CreatedAtMixin, Base):
    """One immutable budget movement (reserve / commit / release)."""

    __tablename__ = "budget_ledger"
    __table_args__ = (
        Index("ix_budget_ledger_scope", "book_id", "session_id", "scene_id"),
        Index("ix_budget_ledger_reservation", "reservation_id"),
        Index("ix_budget_ledger_kind", "kind"),
    )

    # All scopes are nullable: a reservation may be global, per-session, and/or
    # per-scene at once. book_id/session_id use SET NULL so the global ceiling
    # accounting survives a book/session deletion.
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    # scene_id is a plain string (not an FK): a render may be reserved before
    # the scene row is persisted, and the cap is a pure windowed sum.
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    kind: Mapped[BudgetKind] = mapped_column(str_enum(BudgetKind, "budget_kind"), nullable=False)
    video_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    # Identifies the reservation this row belongs to (a reserve row points at
    # itself; commit/release rows point at their reserve row).
    reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
