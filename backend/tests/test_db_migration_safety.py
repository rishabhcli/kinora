"""Unit tests for the migration-safety toolkit (SQL generation + linter; no infra).

The backfill runner is exercised with injected execute/commit callables so the
batching/cursor-advance logic is testable without a live engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.migration_safety import (
    BackfillRunner,
    add_foreign_key_not_valid_sql,
    add_nullable_column_sql,
    assert_ddl_safe,
    create_index_concurrently_sql,
    drop_index_concurrently_sql,
    lint_ddl,
    set_lock_timeout_sql,
    set_not_null_via_check_sql,
    set_statement_timeout_sql,
)


def test_lock_timeout_sql() -> None:
    assert set_lock_timeout_sql(2500) == "SET lock_timeout = '2500ms'"
    assert set_statement_timeout_sql(0) == "SET statement_timeout = '0ms'"
    with pytest.raises(ValueError, match=">= 0"):
        set_lock_timeout_sql(-1)


def test_create_index_concurrently_sql_variants() -> None:
    basic = create_index_concurrently_sql(
        index_name="ix_pages_book", table="pages", columns=["book_id", "page_number"]
    )
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pages_book" in basic
    assert "ON pages (book_id, page_number)" in basic

    unique_partial = create_index_concurrently_sql(
        index_name="ux_active",
        table="books",
        columns=["user_id"],
        unique=True,
        where="deleted_at IS NULL",
    )
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in unique_partial
    assert "WHERE deleted_at IS NULL" in unique_partial

    gin = create_index_concurrently_sql(
        index_name="gin_meta", table="shots", columns=["meta"], method="gin"
    )
    assert "USING gin" in gin

    with pytest.raises(ValueError, match="at least one column"):
        create_index_concurrently_sql(index_name="x", table="t", columns=[])


def test_drop_index_concurrently_sql() -> None:
    assert drop_index_concurrently_sql("ix_x") == "DROP INDEX CONCURRENTLY IF EXISTS ix_x"


def test_add_nullable_column_sql() -> None:
    sql = add_nullable_column_sql("pages", "word_count", "integer")
    assert sql == "ALTER TABLE pages ADD COLUMN IF NOT EXISTS word_count integer NULL"


def test_set_not_null_via_check_sequence() -> None:
    stmts = set_not_null_via_check_sql("pages", "word_count")
    assert len(stmts) == 4
    assert "NOT VALID" in stmts[0]
    assert "VALIDATE CONSTRAINT" in stmts[1]
    assert "SET NOT NULL" in stmts[2]
    assert "DROP CONSTRAINT" in stmts[3]


def test_add_foreign_key_not_valid_sequence() -> None:
    stmts = add_foreign_key_not_valid_sql(
        table="shots", constraint="fk_shots_book", column="book_id", ref_table="books"
    )
    assert "NOT VALID" in stmts[0]
    assert "REFERENCES books (id)" in stmts[0]
    assert "VALIDATE CONSTRAINT fk_shots_book" in stmts[1]


def test_lint_ddl_flags_blocking_index() -> None:
    findings = lint_ddl("CREATE INDEX ix_x ON pages (book_id)")
    assert any(f.rule == "blocking_create_index" and f.severity == "danger" for f in findings)
    # The concurrent form is clean.
    assert lint_ddl("CREATE INDEX CONCURRENTLY ix_x ON pages (book_id)") == []


def test_lint_ddl_flags_set_not_null_and_fk() -> None:
    nn = lint_ddl("ALTER TABLE pages ALTER COLUMN word_count SET NOT NULL")
    assert any(f.rule == "blocking_set_not_null" for f in nn)

    fk = lint_ddl("ALTER TABLE shots ADD CONSTRAINT fk FOREIGN KEY (book_id) REFERENCES books (id)")
    assert any(f.rule == "blocking_add_fk" for f in fk)


def test_lint_ddl_flags_unbounded_dml() -> None:
    findings = lint_ddl("UPDATE pages SET word_count = 0")
    assert any(f.rule == "unbounded_dml" for f in findings)
    # A bounded update is clean.
    assert lint_ddl("UPDATE pages SET word_count = 0 WHERE id = 'x'") == []


def test_lint_ddl_add_column_default_not_null() -> None:
    findings = lint_ddl("ALTER TABLE pages ADD COLUMN flag boolean NOT NULL DEFAULT false")
    assert any(f.rule == "add_column_default_not_null" for f in findings)


def test_assert_ddl_safe_raises_on_danger() -> None:
    with pytest.raises(ValueError, match="unsafe DDL"):
        assert_ddl_safe("CREATE INDEX ix ON t (c)")
    # A warning-only statement does not raise.
    assert_ddl_safe(
        "ALTER TABLE shots ADD CONSTRAINT fk FOREIGN KEY (book_id) REFERENCES books (id)"
    )


def test_backfill_runner_rejects_bad_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        BackfillRunner(update_sql="UPDATE ...", batch_size=0)


async def test_backfill_runner_iterates_until_empty() -> None:
    # Simulate 5 rows processed in batches of 2: returns [k1,k2], [k3,k4], [k5], [].
    pages = [[1, 2], [3, 4], [5], []]
    calls: list[dict[str, Any]] = []
    commits = {"n": 0}

    async def execute(sql: str, params: dict[str, Any]) -> list[Any]:
        calls.append(params)
        return pages[len(calls) - 1]

    async def commit() -> None:
        commits["n"] += 1

    runner = BackfillRunner(update_sql="UPDATE t ... RETURNING id", batch_size=2)
    report = await runner.run(commit, execute, start_after=0)

    assert report.done is True
    assert report.rows_updated == 5
    assert report.batches == 4  # 3 with rows + 1 empty terminator
    assert commits["n"] == 4
    # Cursor advanced by the max key of each batch.
    assert [c["after"] for c in calls] == [0, 2, 4, 5]
    assert all(c["limit"] == 2 for c in calls)


async def test_backfill_runner_respects_max_batches() -> None:
    async def execute(sql: str, params: dict[str, Any]) -> list[Any]:
        return [1, 2, 3]  # never empties

    async def commit() -> None:
        return None

    runner = BackfillRunner(update_sql="UPDATE ...", batch_size=3, max_batches=2)
    report = await runner.run(commit, execute, start_after=0)
    assert report.batches == 2
    assert report.done is False
