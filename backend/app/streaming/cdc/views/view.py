"""The materialised-view contract + a batteries-included base.

A :class:`MaterializedView` declares which source tables feed it, computes the
**delta** to its materialised state from one change event, and holds that state
as a :class:`~app.streaming.cdc.views.delta.ZSet`. The engine drives it; the
view never reads the stream itself.

Two layers:

* :class:`MaterializedView` — the abstract contract (``sources``, ``on_event``,
  ``state``, ``recompute``). ``recompute`` is the *consistency oracle*: given the
  full set of source rows it returns what the view *should* be, independent of
  the incremental path. The engine uses it to assert that the incrementally
  maintained state matches a from-scratch computation (IVM correctness).
* :class:`KeyedProjectionView` — the common case: a 1:1 (or 1:0) projection of
  one base table's rows into denormalised output rows, keyed by primary key.
  Subclasses implement ``project(row) -> output dict | None`` and get correct
  insert/update/delete delta handling for free.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Mapping
from typing import Any

from app.streaming.cdc.events import ChangeEvent, Op, key_str
from app.streaming.cdc.views.delta import Delta, Row, ZSet, update_delta


class MaterializedView(abc.ABC):
    """A denormalised read model maintained incrementally from change events."""

    #: Stable view name (also its node id in the dependency graph).
    name: str = "view"

    @property
    @abc.abstractmethod
    def sources(self) -> tuple[str, ...]:
        """The source table names this view depends on."""
        raise NotImplementedError

    @abc.abstractmethod
    def on_event(self, event: ChangeEvent) -> Delta:
        """Return the delta this event applies to the view's state.

        Must be pure w.r.t. ``event`` and the view's current state; the engine
        applies the returned delta to :attr:`state` and records it for fan-out.
        """
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def state(self) -> ZSet:
        """The current materialised content as a Z-set."""
        raise NotImplementedError

    def apply(self, delta: Delta) -> None:
        """Fold ``delta`` into the view's state (the engine calls this)."""
        self.state.__iadd__(delta)

    # -- consistency oracle ------------------------------------------------- #
    @abc.abstractmethod
    def recompute(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> ZSet:
        """Compute the view from scratch given all live rows of each source.

        ``base`` maps source table -> iterable of current row dicts. Returns the
        Z-set the view *should* equal; the engine compares this against the
        incrementally maintained :attr:`state` for the consistency check.
        """
        raise NotImplementedError

    # -- read helpers ------------------------------------------------------- #
    def rows(self) -> list[dict[str, Any]]:
        """Materialised output rows as plain dicts (the read API)."""
        return [r.as_dict() for r in self.state.rows()]


class KeyedProjectionView(MaterializedView):
    """A per-row projection of a single base table, keyed by primary key.

    Subclasses implement :meth:`project`; this base turns insert/update/delete
    events into the correct retract-old/assert-new deltas, remembering the last
    projected row per key so an UPDATE (whose before-image the polling source
    can't supply) still retracts the prior output.
    """

    def __init__(self) -> None:
        self._state = ZSet()
        #: key string -> last projected Row we asserted (for retraction on update)
        self._projected: dict[str, Row] = {}

    @property
    def state(self) -> ZSet:
        return self._state

    @property
    @abc.abstractmethod
    def source(self) -> str:
        """The single base table this view projects."""
        raise NotImplementedError

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.source,)

    @abc.abstractmethod
    def project(self, row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Map a base row to its denormalised output row, or ``None`` to omit it."""
        raise NotImplementedError

    def on_event(self, event: ChangeEvent) -> Delta:
        if event.table != self.source or not event.is_row_event:
            return ZSet()
        k = key_str(event.key)
        old = self._projected.get(k)

        if event.is_delete:
            if old is None:
                return ZSet()
            del self._projected[k]
            return update_delta(old, None)

        # insert / update / snapshot-read all assert the projected row.
        body = event.after or {}
        projected = self.project(body)
        new = Row(projected) if projected is not None else None
        if new is None:
            # The row no longer qualifies (e.g. soft-deleted via a flag).
            if old is None:
                return ZSet()
            del self._projected[k]
            return update_delta(old, None)
        self._projected[k] = new
        return update_delta(old, new)

    def recompute(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> ZSet:
        out = ZSet()
        for row in base.get(self.source, []):
            projected = self.project(row)
            if projected is not None:
                out.add(Row(projected), +1)
        return out


__all__ = ["KeyedProjectionView", "MaterializedView", "Op"]
