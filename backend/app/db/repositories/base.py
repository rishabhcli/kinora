"""Shared repository base.

Repositories wrap an :class:`AsyncSession` and hold the queries for one
aggregate. They *flush* (to populate defaults / surface constraint errors and
make rows queryable within the transaction) but never *commit* — the unit of
work boundary (:func:`app.db.session.get_session`) owns the transaction.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Base class holding the active :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
