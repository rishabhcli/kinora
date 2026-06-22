"""Declarative base, Alembic naming convention, and shared column mixins.

A fixed :data:`NAMING_CONVENTION` is attached to the metadata so that every
index, unique/check/foreign-key constraint, and primary key gets a deterministic
name. This is what makes Alembic autogenerate stable across machines and over
time (unnamed constraints otherwise get backend-dependent names and produce
spurious migration diffs).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable, explicit naming for all constraints/indexes (see module docstring).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every Kinora ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_id() -> str:
    """Generate a fresh opaque string id (32-char hex UUID4).

    String primary keys let the ingest pipeline assign semantically meaningful
    ids where the design calls for them (``shot_00042``, ``sess_7af3``) while
    still providing a collision-free default for rows created without one.
    """
    return uuid.uuid4().hex


class StrIdMixin:
    """Primary key as an opaque/semantic string id with a UUID4 default."""

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)


class CreatedAtMixin:
    """Server-side ``created_at`` timestamp (UTC, timezone-aware)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    """``created_at`` plus a ``updated_at`` that refreshes on every UPDATE."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
