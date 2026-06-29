"""Durable persistence for the CDC plane (additive tables; migration ``cdc_0001``).

Two small tables let a long-running connector survive restarts:

* ``cdc_offsets`` — the committed :class:`LogPosition` per ``(connector, table)``;
  a restart resumes strictly after it (the snapshot+stream cutover point).
* ``cdc_view_state`` — an optional persisted snapshot of a materialised view's
  content keyed by ``(view, row_key)`` with the Z-set weight, so a view can be
  rehydrated without replaying the whole log (a periodic checkpoint of the
  in-memory engine state).

Both are *standalone* — no foreign keys into the operational schema — because
the CDC plane must keep working (and resuming) even as the rows it mirrors are
created and deleted. They register on ``Base.metadata`` via the package model
registry import; the schema itself is applied by Alembic revision ``cdc_0001``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin


class CdcOffset(StrIdMixin, TimestampMixin, Base):
    """The committed change-log position for one connector/table."""

    __tablename__ = "cdc_offsets"
    __table_args__ = (UniqueConstraint("connector", "table_name"),)

    connector: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: ``__all__`` for a whole-connector offset, or a specific source table.
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: :class:`LogPosition` components — totally ordered ``(major, minor)``.
    position_major: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    position_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Free-form connector metadata (slot name, snapshot phase, ...).
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class CdcViewStateRow(StrIdMixin, TimestampMixin, Base):
    """One persisted row of a materialised view's Z-set state (a checkpoint)."""

    __tablename__ = "cdc_view_state"
    __table_args__ = (UniqueConstraint("view_name", "row_key"),)

    view_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: A stable string key for the materialised row (see ``key_str``).
    row_key: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The Z-set weight (>0 present, the bag multiplicity).
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The denormalised row payload the read API serves.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


__all__ = ["CdcOffset", "CdcViewStateRow"]
