"""Unit tests for SQL fingerprinting + normalisation (no infra)."""

from __future__ import annotations

from app.datascale.optimize.fingerprint import (
    fingerprint,
    make_fingerprint,
    normalize_sql,
    referenced_tables,
)


def test_normalize_collapses_literals_and_case() -> None:
    a = normalize_sql("SELECT * FROM Books WHERE id = 42")
    b = normalize_sql("select  *  from books   where id = 99")
    assert a == b == "select * from books where id = ?"


def test_normalize_handles_string_and_param_markers() -> None:
    skeleton = normalize_sql("SELECT id FROM shot WHERE status = 'rendered' AND book_id = $1")
    assert skeleton == "select id from shot where status = ? and book_id = ?"


def test_normalize_collapses_in_list_arity() -> None:
    two = normalize_sql("SELECT * FROM t WHERE id IN (1, 2)")
    many = normalize_sql("SELECT * FROM t WHERE id IN (1, 2, 3, 4, 5, 6)")
    assert two == many == "select * from t where id in (?)"


def test_normalize_collapses_multi_row_values() -> None:
    skeleton = normalize_sql("INSERT INTO t (a, b) VALUES (1, 2), (3, 4), (5, 6)")
    # The multi-row VALUES tail folds to a single row shape.
    assert "values (?, ?)" in skeleton
    assert skeleton.count("values") == 1


def test_normalize_strips_comments() -> None:
    skeleton = normalize_sql("SELECT 1 -- a comment\nFROM t /* block */ WHERE x = 1")
    assert "comment" not in skeleton
    assert "block" not in skeleton


def test_normalize_is_idempotent() -> None:
    once = normalize_sql("SELECT * FROM books WHERE id = 'abc' AND n = 3")
    twice = normalize_sql(once)
    assert once == twice


def test_normalize_empty() -> None:
    assert normalize_sql("") == ""
    assert normalize_sql("   ") == ""


def test_fingerprint_stable_and_param_insensitive() -> None:
    fp1 = fingerprint("SELECT * FROM books WHERE id = 1")
    fp2 = fingerprint("select * from books where id = 2")
    assert fp1 == fp2
    assert len(fp1) == 40  # sha1 hex


def test_fingerprint_distinguishes_different_shapes() -> None:
    a = fingerprint("SELECT * FROM books WHERE id = 1")
    b = fingerprint("SELECT * FROM shots WHERE id = 1")
    assert a != b


def test_make_fingerprint_short() -> None:
    qf = make_fingerprint("SELECT * FROM books WHERE id = 1")
    assert qf.hexdigest.startswith(qf.short)
    assert len(qf.short) == 12
    assert qf.skeleton == "select * from books where id = ?"


def test_referenced_tables_from_join_into_update() -> None:
    assert referenced_tables("SELECT * FROM books b JOIN shots s ON s.book_id = b.id") == frozenset(
        {"books", "shots"}
    )
    assert referenced_tables("UPDATE shot SET status = 'x' WHERE id = 1") == frozenset({"shot"})
    assert referenced_tables("INSERT INTO entity (a) VALUES (1)") == frozenset({"entity"})


def test_referenced_tables_schema_qualified() -> None:
    assert referenced_tables("SELECT * FROM public.books") == frozenset({"public.books"})
