"""Reusable declarative column mixins: soft-delete, audit, optimistic version.

These extend the small set already in :mod:`app.db.base` (``StrIdMixin``,
``CreatedAtMixin``, ``TimestampMixin``). They are **opt-in**: a model adopts one
by listing it in its bases. Nothing here is applied to existing models — the
mixins are infrastructure that owners can adopt when a new aggregate needs the
behaviour. Each is designed to be composable with the existing mixins.

* :class:`SoftDeleteMixin` — a nullable ``deleted_at`` so a row can be *retired*
  without a physical ``DELETE``. Pairs with :func:`not_deleted` /
  :func:`only_deleted` query predicates and the generic repository's
  soft-delete-aware reads. This is the row-level analogue of the §8.5 "forgetting
  is scoping, not deletion" principle: the row survives for time-travel/audit but
  drops out of the live set.
* :class:`AuditMixin` — ``created_by`` / ``updated_by`` actor columns to record
  *who* mutated a row (the §12.5 observability + the §8.6 preference-learning
  write-back both want attributable edits). Composable with ``TimestampMixin``
  for the *when*.
* :class:`VersionedMixin` — a ``version_id`` integer column wired as SQLAlchemy's
  native ``version_id_col`` so a concurrent overwrite raises
  :class:`~sqlalchemy.orm.exc.StaleDataError` instead of silently clobbering
  (optimistic concurrency control). The canon is heavily versioned (§8.1); this
  gives non-bitemporal aggregates the same lost-update protection cheaply.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, DateTime, Integer, String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class SoftDeleteMixin:
    """Add a nullable ``deleted_at`` marking a row as logically deleted.

    ``deleted_at IS NULL`` means *live*; a timestamp means *retired at that time*.
    Use :func:`not_deleted` in queries to hide retired rows; the generic
    repository hides them by default and exposes ``include_deleted=True`` to opt
    back in (audits, restores, time-travel).
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        """True when this row has been soft-deleted."""
        return self.deleted_at is not None


def not_deleted(model: type[Any]) -> ColumnElement[bool]:
    """Predicate selecting only *live* (not soft-deleted) rows of ``model``."""
    return model.deleted_at.is_(None)


def only_deleted(model: type[Any]) -> ColumnElement[bool]:
    """Predicate selecting only soft-deleted rows of ``model`` (for audits/restores)."""
    return model.deleted_at.is_not(None)


class AuditMixin:
    """Record the actor that created and last updated a row.

    Both columns are nullable opaque ids (an actor may be a user id, a worker
    role, or ``None`` for system writes). Pair with ``TimestampMixin`` for the
    *when*; this carries the *who*. The generic repository / unit of work can
    stamp these from an ambient actor when one is supplied.
    """

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)


class VersionedMixin:
    """Optimistic-concurrency ``version_id`` (SQLAlchemy ``version_id_col``).

    SQLAlchemy increments ``version_id`` on every flush of a dirty row and adds
    ``WHERE version_id = :expected`` to the UPDATE. If another transaction bumped
    it first the UPDATE matches zero rows and SQLAlchemy raises
    :class:`~sqlalchemy.orm.exc.StaleDataError` — the lost-update is caught
    instead of silently overwriting. Adopters get this by listing the mixin and
    nothing else; the ``__mapper_args__`` below registers the column.
    """

    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805 - SQLAlchemy directive
        return {"version_id_col": cls.version_id}


__all__ = [
    "AuditMixin",
    "SoftDeleteMixin",
    "VersionedMixin",
    "not_deleted",
    "only_deleted",
]
