"""The materialised-view engine — routing, maintenance, consistency.

The engine is itself a :class:`~app.streaming.cdc.sink.ChangeSink`: hand it to
the pipeline and every change event flows in via :meth:`emit`. It:

1. **routes** each event to the views whose sources include the event's table
   (and, for view-of-view, the transitive dependents — via the
   :class:`~app.streaming.cdc.views.graph.DependencyGraph`),
2. **maintains** each affected view incrementally by folding the delta returned
   by :meth:`MaterializedView.on_event` into the view's state, in topological
   order so an upstream view is updated before a view that reads it,
3. tracks the **applied position** (the highest event position folded in) so the
   engine's state corresponds to an exact point in the change log, and
4. exposes a **consistency check** (:meth:`verify`) that recomputes every view
   from a supplied base snapshot and asserts the incrementally maintained state
   equals the from-scratch result — the IVM correctness guarantee.

The engine is provider-agnostic and infra-free: deterministic tests drive it
straight from a :class:`~app.streaming.cdc.source.FakeChangeStream`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.streaming.cdc.events import ChangeEvent, LogPosition, Op
from app.streaming.cdc.views.delta import Delta, ZSet
from app.streaming.cdc.views.graph import DependencyGraph
from app.streaming.cdc.views.view import MaterializedView


@dataclass(slots=True)
class ViewConsistency:
    """The result of a :meth:`MaterializedViewEngine.verify` for one view."""

    view: str
    consistent: bool
    #: Rows the incremental state has that a from-scratch recompute does not.
    extra: int = 0
    #: Rows the recompute has that the incremental state is missing.
    missing: int = 0


@dataclass(slots=True)
class EngineStats:
    """Observable counters (feeds §12.5 observability / the metrics panel)."""

    events_applied: int = 0
    deltas_emitted: int = 0
    snapshot_rows: int = 0
    per_view: dict[str, int] = field(default_factory=dict)
    applied_position: LogPosition = LogPosition.zero()


class MaterializedViewEngine:
    """Registers views and maintains them incrementally as a :class:`ChangeSink`."""

    def __init__(self) -> None:
        self._views: dict[str, MaterializedView] = {}
        self._graph = DependencyGraph()
        # source table -> directly-feeding view names
        self._by_source: dict[str, list[str]] = {}
        self.stats = EngineStats()

    # -- registration ------------------------------------------------------- #
    def register(self, view: MaterializedView) -> MaterializedView:
        """Add ``view`` and wire its source/dependency edges. Returns the view."""
        if view.name in self._views:
            raise ValueError(f"view {view.name!r} already registered")
        self._views[view.name] = view
        self._graph.add_view(view.name, view.sources)
        for src in view.sources:
            self._by_source.setdefault(src, []).append(view.name)
        self.stats.per_view.setdefault(view.name, 0)
        return view

    def view(self, name: str) -> MaterializedView:
        return self._views[name]

    @property
    def graph(self) -> DependencyGraph:
        return self._graph

    # -- ChangeSink interface ---------------------------------------------- #
    async def emit(self, event: ChangeEvent) -> None:
        """Apply one change event to every affected view (ChangeSink contract)."""
        self.apply(event)

    def apply(self, event: ChangeEvent) -> list[tuple[str, Delta]]:
        """Synchronously maintain views for ``event``; return ``(view, delta)`` list.

        Heartbeats only advance the position. Schema events are not view-bearing
        here (the engine consumes already-migrated rows); the schema registry
        upstream handles them. Row events route by table and apply in
        topological order.
        """
        if event.position > self.stats.applied_position:
            self.stats.applied_position = event.position

        if event.op in (Op.HEARTBEAT, Op.SCHEMA):
            return []
        if not event.is_row_event:
            return []

        self.stats.events_applied += 1
        affected = self._affected_views(event.table)
        applied: list[tuple[str, Delta]] = []
        for view_name in affected:
            view = self._views[view_name]
            delta = view.on_event(event)
            if delta:
                view.apply(delta)
                self.stats.deltas_emitted += 1
                self.stats.per_view[view_name] = self.stats.per_view.get(view_name, 0) + 1
                applied.append((view_name, delta))
        if event.is_snapshot:
            self.stats.snapshot_rows += 1
        return applied

    def _affected_views(self, table: str) -> list[str]:
        """Direct + transitive views for ``table``, in topological order."""
        direct = set(self._by_source.get(table, []))
        transitive = self._graph.dirty_views([table])
        targets = direct | transitive
        if not targets:
            return []
        return [v for v in self._graph.topological_order() if v in targets]

    # -- reads -------------------------------------------------------------- #
    def rows(self, view: str) -> list[dict[str, Any]]:
        return self._views[view].rows()

    def state(self, view: str) -> ZSet:
        return self._views[view].state

    # -- consistency oracle ------------------------------------------------- #
    def verify(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, ViewConsistency]:
        """Recompute every view from ``base`` and compare to incremental state.

        ``base`` maps source table -> all currently-live rows. Returns a per-view
        :class:`ViewConsistency`. ``all(c.consistent ...)`` is the assertion an
        IVM correctness test makes.
        """
        results: dict[str, ViewConsistency] = {}
        for name, view in self._views.items():
            expected = view.recompute(base)
            actual = view.state
            results[name] = self._compare(name, expected, actual)
        return results

    @staticmethod
    def _compare(name: str, expected: ZSet, actual: ZSet) -> ViewConsistency:
        exp_rows = {r: expected.weight(r) for r in expected.rows()}
        act_rows = {r: actual.weight(r) for r in actual.rows()}
        extra = sum(1 for r, w in act_rows.items() if exp_rows.get(r, 0) != w)
        missing = sum(1 for r, w in exp_rows.items() if act_rows.get(r, 0) != w)
        return ViewConsistency(
            view=name,
            consistent=(exp_rows == act_rows) and actual.is_consistent(),
            extra=extra,
            missing=missing,
        )


__all__ = ["EngineStats", "MaterializedViewEngine", "ViewConsistency"]
