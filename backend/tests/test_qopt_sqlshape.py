"""Unit tests for the SQL shape parser (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.optimize.errors import ParseError
from app.datascale.optimize.sqlshape import (
    ColumnRef,
    PredicateOp,
    parse_select,
    try_parse_select,
)


def test_simple_select_one_table_eq() -> None:
    shape = parse_select("SELECT id, title FROM books WHERE id = $1")
    assert shape.table_names == frozenset({"books"})
    assert [c.column for c in shape.columns] == ["id", "title"]
    assert not shape.star
    assert len(shape.predicates) == 1
    p = shape.predicates[0]
    assert p.column == ColumnRef(None, "id")
    assert p.op is PredicateOp.EQ
    assert p.literal_rhs


def test_select_star() -> None:
    shape = parse_select("SELECT * FROM shot")
    assert shape.star
    assert shape.columns == []
    assert shape.table_names == frozenset({"shot"})


def test_equality_and_range_columns() -> None:
    shape = parse_select(
        "SELECT id FROM shot WHERE book_id = $1 AND page_no >= 10 AND page_no < 20"
    )
    eq = {c.column for c in shape.equality_columns()}
    rng = {c.column for c in shape.range_columns()}
    assert eq == {"book_id"}
    assert rng == {"page_no"}


def test_in_predicate_is_equality_seek() -> None:
    shape = parse_select("SELECT id FROM shot WHERE book_id IN (1, 2, 3)")
    assert {c.column for c in shape.equality_columns()} == {"book_id"}


def test_join_condition_extracted() -> None:
    shape = parse_select(
        "SELECT b.id FROM books b JOIN shots s ON s.book_id = b.id WHERE b.id = 1"
    )
    assert shape.table_names == frozenset({"books", "shots"})
    assert len(shape.joins) == 1
    jc = shape.joins[0]
    assert {jc.left.column, jc.right.column} == {"book_id", "id"}


def test_join_via_where_clause() -> None:
    shape = parse_select(
        "SELECT b.id FROM books b, shots s WHERE s.book_id = b.id AND b.id = 1"
    )
    assert len(shape.joins) == 1
    # b.id = 1 is a literal predicate, not a join.
    assert len(shape.predicates) == 1
    assert shape.predicates[0].column == ColumnRef("b", "id")


def test_group_by_and_aggregate() -> None:
    shape = parse_select(
        "SELECT book_id, count(*) FROM shot GROUP BY book_id"
    )
    assert shape.is_aggregate
    assert "count" in shape.aggregates
    assert [c.column for c in shape.group_by] == ["book_id"]


def test_order_by_and_limit() -> None:
    shape = parse_select("SELECT id FROM shot ORDER BY created_at DESC LIMIT 50")
    assert [c.column for c in shape.order_by] == ["created_at"]
    assert shape.limit == 50


def test_distinct() -> None:
    shape = parse_select("SELECT DISTINCT book_id FROM shot")
    assert shape.distinct


def test_is_null_predicate() -> None:
    shape = parse_select("SELECT id FROM shot WHERE deleted_at IS NULL")
    assert shape.predicates[0].op is PredicateOp.IS_NULL


def test_between_is_range() -> None:
    shape = parse_select("SELECT id FROM shot WHERE page_no BETWEEN 1 AND 9")
    assert {c.column for c in shape.range_columns()} == {"page_no"}


def test_like_predicate() -> None:
    shape = parse_select("SELECT id FROM books WHERE title LIKE 'foo%'")
    assert shape.predicates[0].op is PredicateOp.LIKE
    # LIKE is neither an equality nor a range seek column.
    assert shape.equality_columns() == []
    assert shape.range_columns() == []


def test_or_predicates_are_not_seek_columns() -> None:
    # Top-level OR: we extract no AND-atom seek columns (conservative).
    shape = parse_select("SELECT id FROM shot WHERE book_id = 1 OR status = 'x'")
    assert shape.equality_columns() == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE books SET x = 1",
        "SELECT 1",  # no FROM
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SELECT * FROM a UNION SELECT * FROM b",
        "SELECT * FROM (SELECT id FROM books) sub",
        "",
    ],
)
def test_unsupported_shapes_raise(sql: str) -> None:
    with pytest.raises(ParseError):
        parse_select(sql)


def test_try_parse_returns_none_on_unsupported() -> None:
    assert try_parse_select("UPDATE books SET x = 1") is None
    assert try_parse_select("SELECT id FROM books") is not None


def test_alias_in_select_list_stripped() -> None:
    shape = parse_select("SELECT id AS book_id, title t FROM books")
    assert [c.column for c in shape.columns] == ["id", "title"]
