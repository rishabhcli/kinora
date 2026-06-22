"""``users`` — accounts for JWT auth (wired in a later phase)."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class User(StrIdMixin, CreatedAtMixin, Base):
    """A registered reader/director."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
