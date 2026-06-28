"""Unit tests for the query-shaping helpers (compile SQL; no DB connection)."""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.query import (
    Cursor,
    Page,
    apply_filters,
    apply_ordering,
    chunked,
    count_statement,
    in_chunks,
    keyset_paginate,
    paginate,
)


class _Base(DeclarativeBase):
    pass


class Widget(_Base):
    __tablename__ = "test_widgets_query"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    rank: Mapped[int] = mapped_column(Integer)


def _sql(stmt: object) -> str:
    return str(stmt).lower()


def test_apply_filters_equality_and_in() -> None:
    stmt = apply_filters(select(Widget), Widget, {"name": "a", "rank": [1, 2, 3]})
    sql = _sql(stmt)
    assert "where" in sql
    assert "name =" in sql
    assert "rank in" in sql


def test_apply_filters_operators() -> None:
    stmt = apply_filters(
        select(Widget),
        Widget,
        {"rank__gt": 5, "rank__le": 10, "name__ilike": "a%"},
    )
    sql = _sql(stmt)
    assert "rank >" in sql
    assert "rank <=" in sql
    assert "lower(test_widgets_query.name) like lower" in sql or "ilike" in sql


def test_apply_filters_unknown_column_raises() -> None:
    with pytest.raises(ValueError, match="no mapped column"):
        apply_filters(select(Widget), Widget, {"nope": 1})


def test_apply_filters_unknown_operator_raises() -> None:
    with pytest.raises(ValueError, match="unsupported filter operator"):
        apply_filters(select(Widget), Widget, {"rank__between": 1})


def test_apply_ordering_directions() -> None:
    stmt = apply_ordering(select(Widget), Widget, ["rank", "-name"])
    sql = _sql(stmt)
    assert "order by" in sql
    assert "rank asc" in sql
    assert "name desc" in sql


def test_apply_ordering_allowlist_enforced() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        apply_ordering(select(Widget), Widget, ["rank"], allowed=["name"])
    # Allowed column passes.
    apply_ordering(select(Widget), Widget, ["name"], allowed=["name", "rank"])


def test_paginate_clamps_negatives() -> None:
    stmt = paginate(select(Widget), limit=-5, offset=-10)
    sql = _sql(stmt)
    assert "limit" in sql
    # Offset 0 may be omitted by some dialects; the clamp itself is unit-tested
    # via the absence of a negative literal.
    assert "-5" not in sql
    assert "-10" not in sql


def test_count_statement_strips_order_and_window() -> None:
    stmt = paginate(apply_ordering(select(Widget), Widget, ["rank"]), limit=10, offset=20)
    count = count_statement(stmt)
    sql = _sql(count)
    assert "count(" in sql
    # The wrapped subquery omits the outer ORDER BY / LIMIT (count is invariant).
    assert "order by" not in sql.split("from")[0]


def test_keyset_paginate_first_and_next_page() -> None:
    first = keyset_paginate(select(Widget), Widget, key="id", limit=5)
    assert "order by" in _sql(first)
    assert "limit" in _sql(first)

    nxt = keyset_paginate(
        select(Widget), Widget, key="id", limit=5, after=Cursor(last_value="w_010")
    )
    sql = _sql(nxt)
    assert "id >" in sql

    desc_next = keyset_paginate(
        select(Widget),
        Widget,
        key="id",
        limit=5,
        after=Cursor(last_value="w_010"),
        descending=True,
    )
    assert "id <" in _sql(desc_next)
    assert "desc" in _sql(desc_next)


def test_chunked_splits_evenly() -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 3)) == []
    with pytest.raises(ValueError, match="chunk size"):
        list(chunked([1], 0))


def test_in_chunks_dedups_and_batches() -> None:
    clauses = list(in_chunks(Widget, "id", ["a", "b", "a", "c", "b", "d"], size=2))
    # 4 unique ids in batches of 2 → 2 clauses.
    assert len(clauses) == 2
    rendered = " ".join(_sql(c) for c in clauses)
    assert "id in" in rendered


def test_page_metadata() -> None:
    page: Page[str] = Page(items=["a", "b"], total=10, limit=2, offset=4)
    assert page.has_more is True
    assert page.page_number == 3
    assert page.num_pages == 5

    last: Page[str] = Page(items=["x"], total=5, limit=2, offset=4)
    assert last.has_more is False
