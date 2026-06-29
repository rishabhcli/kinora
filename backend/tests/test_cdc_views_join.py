"""Tests for the incremental equi-join view (no infra).

Covers delta propagation from both sides, update/key-change moves, and the
from-scratch consistency oracle after churn.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.streaming.cdc.events import ChangeEvent, LogPosition
from app.streaming.cdc.views import EquiJoinView
from app.streaming.cdc.views.engine import MaterializedViewEngine


class _Pos:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> LogPosition:
        self.n += 1
        return LogPosition(self.n, 0)


def _book_pages_join() -> EquiJoinView:
    return EquiJoinView(
        name="book_pages",
        left_table="books",
        right_table="pages",
        left_on="id",
        right_on="book_id",
        combine=lambda b, p: {
            "book_id": b["id"],
            "title": b.get("title"),
            "page_id": p["id"],
            "page_number": p.get("page_number"),
        },
    )


def test_join_emits_matched_pairs() -> None:
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "Dune"}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg2", "book_id": "b1", "page_number": 2}, p()))
    rows = sorted(engine.rows("book_pages"), key=lambda r: r["page_number"])
    assert len(rows) == 2
    assert all(r["title"] == "Dune" for r in rows)
    assert [r["page_number"] for r in rows] == [1, 2]


def test_join_left_side_arrives_after_right() -> None:
    # Right rows present first, then the left arrives → all pairs emit.
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    engine.apply(ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    assert engine.rows("book_pages") == []  # no left match yet
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "Dune"}, p()))
    assert len(engine.rows("book_pages")) == 1


def test_join_left_update_propagates_to_all_matches() -> None:
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "Dune"}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg2", "book_id": "b1", "page_number": 2}, p()))
    # Retitle the book → both joined rows update.
    engine.apply(ChangeEvent.update("books", None, {"id": "b1", "title": "Dune Messiah"}, p()))
    rows = engine.rows("book_pages")
    assert len(rows) == 2
    assert all(r["title"] == "Dune Messiah" for r in rows)


def test_join_right_delete_retracts_pair() -> None:
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "Dune"}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    engine.apply(ChangeEvent.delete("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    assert engine.rows("book_pages") == []
    assert engine.state("book_pages").is_consistent()


def test_join_key_change_moves_pairs() -> None:
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    engine.apply(ChangeEvent.insert("books", {"id": "b1", "title": "A"}, p()))
    engine.apply(ChangeEvent.insert("books", {"id": "b2", "title": "B"}, p()))
    engine.apply(ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()))
    # Move the page to b2 (join-key change on the right side).
    engine.apply(
        ChangeEvent.update("pages", None, {"id": "pg1", "book_id": "b2", "page_number": 1}, p())
    )
    rows = engine.rows("book_pages")
    assert len(rows) == 1
    assert rows[0]["title"] == "B"


def test_join_consistency_oracle_after_churn() -> None:
    engine = MaterializedViewEngine()
    engine.register(_book_pages_join())
    p = _Pos()
    events = [
        ChangeEvent.insert("books", {"id": "b1", "title": "A"}, p()),
        ChangeEvent.insert("books", {"id": "b2", "title": "B"}, p()),
        ChangeEvent.insert("pages", {"id": "pg1", "book_id": "b1", "page_number": 1}, p()),
        ChangeEvent.insert("pages", {"id": "pg2", "book_id": "b1", "page_number": 2}, p()),
        ChangeEvent.insert("pages", {"id": "pg3", "book_id": "b2", "page_number": 1}, p()),
        ChangeEvent.update("pages", None, {"id": "pg2", "book_id": "b2", "page_number": 2}, p()),
        ChangeEvent.delete("books", {"id": "b1", "title": "A"}, p()),
    ]
    for e in events:
        engine.apply(e)
    base: Mapping[str, Iterable[Mapping[str, Any]]] = {
        "books": [{"id": "b2", "title": "B"}],
        "pages": [
            {"id": "pg1", "book_id": "b1", "page_number": 1},  # orphan (b1 gone)
            {"id": "pg2", "book_id": "b2", "page_number": 2},
            {"id": "pg3", "book_id": "b2", "page_number": 1},
        ],
    }
    result = engine.verify(base)
    assert result["book_pages"].consistent
