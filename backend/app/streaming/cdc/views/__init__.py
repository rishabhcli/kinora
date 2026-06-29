"""Incrementally-maintained materialised views over the change stream.

This sub-package turns the raw change stream into denormalised read models that
are kept consistent with the source *incrementally* — each change event applies
a small delta to the affected view rather than recomputing it. See
``../DESIGN.md`` for the architecture.

Public surface:

* :class:`~app.streaming.cdc.views.delta.Row` / :class:`Delta` / :class:`ZSet`
  — the delta algebra (weighted multiset) IVM is built on.
* :class:`~app.streaming.cdc.views.view.MaterializedView` — the view ABC.
* :class:`~app.streaming.cdc.views.engine.MaterializedViewEngine` — registration,
  routing, incremental maintenance, and consistency checks.
* :class:`~app.streaming.cdc.views.graph.DependencyGraph` — view/table dependency
  topology for ordered, dirty-driven refresh.
* View kinds: :class:`KeyedProjectionView` (1:1 projection),
  :class:`AggregateView` (GROUP BY + invertible reducers), :class:`EquiJoinView`
  (symmetric-hash incremental join).
* Concrete views: :class:`LibraryShelfView`, :class:`CanonGraphView`.
"""

from __future__ import annotations

from app.streaming.cdc.views.aggregate import (
    AggregateView,
    AvgReducer,
    CountReducer,
    DistinctCountReducer,
    MaxReducer,
    MinReducer,
    Reducer,
    SumReducer,
)
from app.streaming.cdc.views.canon_graph import CanonGraphView
from app.streaming.cdc.views.delta import Delta, Row, ZSet
from app.streaming.cdc.views.engine import MaterializedViewEngine, ViewConsistency
from app.streaming.cdc.views.graph import DependencyGraph
from app.streaming.cdc.views.join import EquiJoinView
from app.streaming.cdc.views.library_shelf import LibraryShelfView
from app.streaming.cdc.views.view import KeyedProjectionView, MaterializedView

__all__ = [
    "AggregateView",
    "AvgReducer",
    "CanonGraphView",
    "CountReducer",
    "Delta",
    "DependencyGraph",
    "DistinctCountReducer",
    "EquiJoinView",
    "KeyedProjectionView",
    "LibraryShelfView",
    "MaterializedView",
    "MaterializedViewEngine",
    "MaxReducer",
    "MinReducer",
    "Reducer",
    "Row",
    "SumReducer",
    "ViewConsistency",
    "ZSet",
]
