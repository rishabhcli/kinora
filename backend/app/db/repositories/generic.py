"""A generic, typed repository the concrete repositories can adopt.

The existing :class:`~app.db.repositories.base.BaseRepository` only holds the
session. :class:`GenericRepository` builds on it with the CRUD + query surface
every aggregate re-implements by hand today: typed ``get``/``create``/``update``/
``delete``, ``list``/``count``/``exists`` with the :mod:`app.db.query` filter and
ordering DSL, offset and keyset pagination, and *soft-delete-aware* reads when
the model uses :class:`~app.db.mixins.SoftDeleteMixin`.

It is deliberately additive: a concrete repository can subclass
``GenericRepository[MyModel, str]`` to inherit the surface and still add its
domain-specific queries, or keep extending ``BaseRepository`` and ignore this
entirely. The contract is unchanged — the repository *flushes*, never *commits*;
the unit of work owns the transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, Select, delete, func, select, update
from sqlalchemy.orm import InstrumentedAttribute

from app.db.mixins import SoftDeleteMixin, not_deleted
from app.db.query import (
    Cursor,
    Page,
    apply_filters,
    apply_ordering,
    count_statement,
    keyset_paginate,
    paginate,
)
from app.db.repositories.base import BaseRepository

ModelT = TypeVar("ModelT")
IdT = TypeVar("IdT")


class GenericRepository(BaseRepository, Generic[ModelT, IdT]):
    """Typed CRUD + query surface for one mapped ``model``.

    Subclasses set the ``model`` class attribute (or pass it to ``__init__``).
    The id type ``IdT`` is whatever the model's primary key is (Kinora uses
    string ids almost everywhere; ``int`` works too).
    """

    #: The mapped class this repository operates on. Subclasses set this; it can
    #: also be passed to ``__init__`` for ad-hoc use without subclassing.
    model: type[ModelT]

    def __init__(self, session: Any, model: type[ModelT] | None = None) -> None:
        super().__init__(session)
        if model is not None:
            self.model = model
        if not hasattr(self, "model"):
            raise TypeError(f"{type(self).__name__} requires a `model` class attribute or argument")

    # -- introspection ------------------------------------------------------- #

    @property
    def supports_soft_delete(self) -> bool:
        """True when the model mixes in :class:`SoftDeleteMixin`."""
        return issubclass(self.model, SoftDeleteMixin)

    def _pk_column(self) -> InstrumentedAttribute[Any]:
        """The single primary-key column (composite PKs aren't supported here)."""
        mapper = cast(Any, self.model).__mapper__
        pk_cols = mapper.primary_key
        if len(pk_cols) != 1:
            raise TypeError(
                f"{self.model.__name__} has a composite primary key; "
                "GenericRepository supports single-column PKs only"
            )
        return getattr(self.model, pk_cols[0].name)

    def _base_select(self, *, include_deleted: bool) -> Select[Any]:
        stmt = select(self.model)
        if self.supports_soft_delete and not include_deleted:
            stmt = stmt.where(not_deleted(self.model))
        return stmt

    # -- create -------------------------------------------------------------- #

    async def create(self, **fields: Any) -> ModelT:
        """Insert a new row from column values; flush to populate defaults."""
        instance = self.model(**fields)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def add(self, instance: ModelT) -> ModelT:
        """Add a pre-built model instance and flush it."""
        self.session.add(instance)
        await self.session.flush()
        return instance

    # -- read ---------------------------------------------------------------- #

    async def get(self, id_: IdT, *, include_deleted: bool = False) -> ModelT | None:
        """Fetch by primary key (hides soft-deleted rows unless asked)."""
        instance = await self.session.get(self.model, id_)
        if instance is None:
            return None
        if (
            not include_deleted
            and self.supports_soft_delete
            and cast(SoftDeleteMixin, instance).is_deleted
        ):
            return None
        return instance

    async def get_or_raise(self, id_: IdT, *, include_deleted: bool = False) -> ModelT:
        """Like :meth:`get` but raise :class:`KeyError` when missing."""
        instance = await self.get(id_, include_deleted=include_deleted)
        if instance is None:
            raise KeyError(f"{self.model.__name__} {id_!r} not found")
        return instance

    async def exists(self, *, include_deleted: bool = False, **filters: Any) -> bool:
        """True when at least one row matches ``filters``."""
        stmt = self._base_select(include_deleted=include_deleted)
        stmt = apply_filters(stmt, self.model, filters).limit(1)
        return (await self.session.execute(stmt)).first() is not None

    async def count(self, *, include_deleted: bool = False, **filters: Any) -> int:
        """Count rows matching ``filters`` (soft-deleted excluded by default)."""
        stmt = self._base_select(include_deleted=include_deleted)
        stmt = apply_filters(stmt, self.model, filters)
        result = await self.session.execute(count_statement(stmt))
        return int(result.scalar_one())

    async def list_rows(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        order_by: Sequence[str] | None = None,
        order_allowed: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> list[ModelT]:
        """List rows with optional filtering, ordering, and a limit/offset window."""
        stmt = self._base_select(include_deleted=include_deleted)
        if filters:
            stmt = apply_filters(stmt, self.model, filters)
        if order_by:
            stmt = apply_ordering(stmt, self.model, order_by, allowed=order_allowed)
        if limit is not None:
            stmt = paginate(stmt, limit=limit, offset=offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def page(
        self,
        *,
        limit: int,
        offset: int = 0,
        filters: Mapping[str, Any] | None = None,
        order_by: Sequence[str] | None = None,
        order_allowed: Sequence[str] | None = None,
        include_deleted: bool = False,
    ) -> Page[ModelT]:
        """Return one :class:`Page` of rows plus the total matching count."""
        total = await self.count(
            include_deleted=include_deleted, **(dict(filters) if filters else {})
        )
        items = await self.list_rows(
            filters=filters,
            order_by=order_by,
            order_allowed=order_allowed,
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    async def keyset_page(
        self,
        *,
        key: str,
        limit: int,
        after: Cursor | None = None,
        descending: bool = False,
        filters: Mapping[str, Any] | None = None,
        include_deleted: bool = False,
    ) -> list[ModelT]:
        """Seek-paginate over a monotonic ``key`` column (O(page size), §4.2-style)."""
        stmt = self._base_select(include_deleted=include_deleted)
        if filters:
            stmt = apply_filters(stmt, self.model, filters)
        stmt = keyset_paginate(
            stmt, self.model, key=key, limit=limit, after=after, descending=descending
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -- update -------------------------------------------------------------- #

    async def update(self, id_: IdT, **fields: Any) -> ModelT | None:
        """Patch columns on one row (by PK); returns the row or ``None``.

        Uses an ORM load so a :class:`~app.db.mixins.VersionedMixin` model's
        optimistic ``version_id`` is honoured on flush (a bulk ``UPDATE`` would
        bypass it).
        """
        instance = await self.get(id_, include_deleted=True)
        if instance is None:
            return None
        for key, value in fields.items():
            setattr(instance, key, value)
        await self.session.flush()
        return instance

    async def update_where(self, values: Mapping[str, Any], /, **filters: Any) -> int:
        """Bulk ``UPDATE ... WHERE`` matching ``filters``; returns rows affected.

        A set-based fast path (no per-row ORM load). It does **not** bump an
        optimistic ``version_id`` — use :meth:`update` for versioned single-row
        edits. Soft-deleted rows are excluded unless ``deleted_at`` is in filters.
        """
        stmt = update(self.model)
        for key, value in filters.items():
            column = getattr(self.model, key)
            stmt = stmt.where(column == value)
        stmt = stmt.values(**values)
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        await self.session.flush()
        return int(result.rowcount or 0)

    # -- delete -------------------------------------------------------------- #

    async def delete(self, id_: IdT) -> bool:
        """Delete one row by PK (soft-delete when the model supports it).

        Returns ``True`` when a row was deleted. For a soft-delete model this
        stamps ``deleted_at`` instead of removing the row.
        """
        if self.supports_soft_delete:
            return await self.soft_delete(id_)
        return await self.hard_delete(id_)

    async def hard_delete(self, id_: IdT) -> bool:
        """Physically ``DELETE`` one row by PK; returns whether it existed."""
        pk = self._pk_column()
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(delete(self.model).where(pk == id_)),
        )
        await self.session.flush()
        return bool(result.rowcount or 0)

    async def soft_delete(self, id_: IdT, *, at: datetime | None = None) -> bool:
        """Stamp ``deleted_at`` on one row (requires :class:`SoftDeleteMixin`)."""
        if not self.supports_soft_delete:
            raise TypeError(f"{self.model.__name__} does not support soft delete")
        instance = await self.get(id_, include_deleted=True)
        if instance is None or cast(SoftDeleteMixin, instance).is_deleted:
            return False
        cast(SoftDeleteMixin, instance).deleted_at = at or datetime.now(UTC)
        await self.session.flush()
        return True

    async def restore(self, id_: IdT) -> bool:
        """Clear ``deleted_at`` on a soft-deleted row; returns whether it changed."""
        if not self.supports_soft_delete:
            raise TypeError(f"{self.model.__name__} does not support soft delete")
        instance = await self.get(id_, include_deleted=True)
        if instance is None or not cast(SoftDeleteMixin, instance).is_deleted:
            return False
        cast(SoftDeleteMixin, instance).deleted_at = None
        await self.session.flush()
        return True

    # -- utility ------------------------------------------------------------- #

    async def refresh(self, instance: ModelT) -> ModelT:
        """Reload an instance's columns from the database (post-commit reads)."""
        await self.session.refresh(cast(Any, instance))
        return instance

    def _max_id_stmt(self) -> Select[Any]:
        """``SELECT max(pk)`` — handy for integer sequences in tests/backfills."""
        return select(func.max(self._pk_column()))


__all__ = ["GenericRepository"]
