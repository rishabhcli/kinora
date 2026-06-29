"""Unit tests for the materialised-view engine + concrete views (no infra).

The recurring assertion is the IVM correctness oracle: drive the engine through
a stream, then ``verify`` that the incrementally maintained state equals a
from-scratch recompute over the final live rows.
"""

from __future__ import annotations

from app.streaming.cdc.events import ChangeEvent, LogPosition
from app.streaming.cdc.views import (
    CanonGraphView,
    DependencyGraph,
    LibraryShelfView,
    MaterializedViewEngine,
)
from app.streaming.cdc.views.graph import DependencyCycleError
from app.streaming.cdc.views.view import KeyedProjectionView


def _pos() -> PosGen:
    return PosGen()


class PosGen:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> LogPosition:
        self.n += 1
        return LogPosition(self.n, 0)


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #
def test_dependency_graph_topo_and_dirty() -> None:
    g = DependencyGraph()
    g.add_view("shelf", ["books"])
    g.add_view("canon", ["entities", "continuity_states"])
    g.add_view("shelf_summary", ["shelf"])  # view-of-view
    order = g.topological_order()
    assert order.index("shelf") < order.index("shelf_summary")
    assert g.dirty_views(["books"]) == {"shelf", "shelf_summary"}
    assert g.dirty_views(["entities"]) == {"canon"}


def test_dependency_cycle_rejected() -> None:
    g = DependencyGraph()
    g.add_view("a", ["b"])
    try:
        g.add_view("b", ["a"])
    except DependencyCycleError:
        return
    raise AssertionError("expected a DependencyCycleError")


# --------------------------------------------------------------------------- #
# Library shelf view
# --------------------------------------------------------------------------- #
def test_library_shelf_insert_update_delete() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    p = _pos()

    engine.apply(
        ChangeEvent.insert("books", {"id": "b1", "title": "Dune", "status": "importing"}, p())
    )
    assert engine.rows("library_shelf") == [
        {
            "book_id": "b1",
            "title": "Dune",
            "author": None,
            "status": "importing",
            "cover_key": None,
            "num_pages": None,
            "owner_id": None,
        }
    ]

    # Status flips importing -> ready: the card updates in place (1 row).
    engine.apply(
        ChangeEvent.update("books", None, {"id": "b1", "title": "Dune", "status": "ready"}, p())
    )
    rows = engine.rows("library_shelf")
    assert len(rows) == 1 and rows[0]["status"] == "ready"

    # Soft-delete drops it off the shelf.
    engine.apply(ChangeEvent.update("books", None, {"id": "b1", "deleted_at": "2026-01-01"}, p()))
    assert engine.rows("library_shelf") == []


def test_library_shelf_consistency_oracle() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    p = _pos()
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "A", "status": "ready"}, p()))
    engine.apply(ChangeEvent.insert("books", {"id": "b2", "title": "B", "status": "ready"}, p()))
    engine.apply(ChangeEvent.delete("books", {"id": "b1", "title": "A"}, p()))

    live = [{"id": "b2", "title": "B", "status": "ready"}]
    result = engine.verify({"books": live})
    assert result["library_shelf"].consistent
    assert result["library_shelf"].extra == 0
    assert result["library_shelf"].missing == 0


# --------------------------------------------------------------------------- #
# Canon graph view (version-aware, multi-source)
# --------------------------------------------------------------------------- #
def test_canon_graph_present_version_supersedes() -> None:
    engine = MaterializedViewEngine()
    engine.register(CanonGraphView())
    p = _pos()

    # v1 of char_elsa is open.
    engine.apply(
        ChangeEvent.insert(
            "entities",
            {
                "id": "e1",
                "book_id": "bk",
                "entity_key": "char_elsa",
                "type": "character",
                "name": "Elsa",
                "version": 1,
                "valid_to_beat": None,
            },
            p(),
            key_columns=("book_id", "entity_key"),
        )
    )
    nodes = [r for r in engine.rows("canon_graph") if r["kind"] == "node"]
    assert len(nodes) == 1 and nodes[0]["version"] == 1

    # v1 is closed (superseded) and v2 inserted open. Present node = v2.
    engine.apply(
        ChangeEvent.update(
            "entities",
            None,
            {
                "id": "e1",
                "book_id": "bk",
                "entity_key": "char_elsa",
                "type": "character",
                "name": "Elsa",
                "version": 1,
                "valid_to_beat": 40,
            },
            p(),
            key_columns=("book_id", "entity_key"),
        )
    )
    engine.apply(
        ChangeEvent.insert(
            "entities",
            {
                "id": "e2",
                "book_id": "bk",
                "entity_key": "char_elsa",
                "type": "character",
                "name": "Elsa the Snow Queen",
                "version": 2,
                "valid_to_beat": None,
            },
            p(),
            key_columns=("book_id", "entity_key"),
        )
    )
    nodes = [r for r in engine.rows("canon_graph") if r["kind"] == "node"]
    assert len(nodes) == 1
    assert nodes[0]["version"] == 2
    assert nodes[0]["name"] == "Elsa the Snow Queen"


def test_canon_graph_active_edges_and_retirement() -> None:
    engine = MaterializedViewEngine()
    engine.register(CanonGraphView())
    p = _pos()
    # An active fact (open interval) → an edge.
    engine.apply(
        ChangeEvent.insert(
            "continuity_states",
            {
                "id": "s1",
                "book_id": "bk",
                "subject": "hero",
                "predicate": "possesses",
                "object": "sword",
                "valid_from_beat": 12,
                "valid_to_beat": None,
            },
            p(),
        )
    )
    edges = [r for r in engine.rows("canon_graph") if r["kind"] == "edge"]
    assert len(edges) == 1

    # retire_state closes the interval → the edge drops out (forgetting, §8.5).
    engine.apply(
        ChangeEvent.update(
            "continuity_states",
            None,
            {
                "id": "s1",
                "book_id": "bk",
                "subject": "hero",
                "predicate": "possesses",
                "object": "sword",
                "valid_from_beat": 12,
                "valid_to_beat": 34,
            },
            p(),
        )
    )
    edges = [r for r in engine.rows("canon_graph") if r["kind"] == "edge"]
    assert edges == []


def test_canon_graph_consistency_oracle() -> None:
    engine = MaterializedViewEngine()
    engine.register(CanonGraphView())
    p = _pos()
    events = [
        ChangeEvent.insert(
            "entities",
            {
                "id": "e1",
                "book_id": "bk",
                "entity_key": "char_a",
                "type": "character",
                "name": "A",
                "version": 1,
                "valid_to_beat": None,
            },
            p(),
            key_columns=("book_id", "entity_key"),
        ),
        ChangeEvent.insert(
            "entities",
            {
                "id": "e2",
                "book_id": "bk",
                "entity_key": "char_a",
                "type": "character",
                "name": "A2",
                "version": 2,
                "valid_to_beat": None,
            },
            p(),
            key_columns=("book_id", "entity_key"),
        ),
        ChangeEvent.insert(
            "continuity_states",
            {
                "id": "s1",
                "book_id": "bk",
                "subject": "A",
                "predicate": "in",
                "object": "castle",
                "valid_from_beat": 1,
                "valid_to_beat": None,
            },
            p(),
        ),
    ]
    for e in events:
        engine.apply(e)

    base = {
        "entities": [
            {
                "id": "e1",
                "book_id": "bk",
                "entity_key": "char_a",
                "type": "character",
                "name": "A",
                "version": 1,
                "valid_to_beat": None,
            },
            {
                "id": "e2",
                "book_id": "bk",
                "entity_key": "char_a",
                "type": "character",
                "name": "A2",
                "version": 2,
                "valid_to_beat": None,
            },
        ],
        "continuity_states": [
            {
                "id": "s1",
                "book_id": "bk",
                "subject": "A",
                "predicate": "in",
                "object": "castle",
                "valid_from_beat": 1,
                "valid_to_beat": None,
            },
        ],
    }
    result = engine.verify(base)
    assert result["canon_graph"].consistent


# --------------------------------------------------------------------------- #
# View-of-view routing
# --------------------------------------------------------------------------- #
class _ReadyOnlyShelf(KeyedProjectionView):
    name = "ready_shelf"

    @property
    def source(self) -> str:
        return "library_shelf"

    def project(self, row):  # type: ignore[no-untyped-def]
        return None


def test_engine_routes_only_affected_views() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    engine.register(CanonGraphView())
    p = _pos()
    applied = engine.apply(
        ChangeEvent.insert("books", {"id": "b1", "title": "x", "status": "ready"}, p())
    )
    # Only the shelf reacts to a books change.
    assert [name for name, _ in applied] == ["library_shelf"]


def test_duplicate_view_registration_rejected() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    try:
        engine.register(LibraryShelfView())
    except ValueError:
        return
    raise AssertionError("expected duplicate registration to raise")
