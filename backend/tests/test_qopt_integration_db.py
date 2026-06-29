"""Postgres-integration tests for the query-optimization platform.

These exercise the parts that need a real database: the EXPLAIN inspector feeding
the profiler + regression guard, and the matview executor applying real DDL +
refreshes. They SKIP when ``KINORA_TEST_DATABASE_URL`` is unset, and otherwise run
against an isolated throwaway database (``qopt_test`` on :5433) — never the live
``kinora`` DB. Each test creates + drops its own scratch tables/views so the suite
leaves no residue.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.datascale.optimize.matview import (
    FreshnessPolicy,
    MatviewDef,
    MatviewExecutor,
    MatviewRegistry,
    RefreshPlanner,
    StalenessClock,
    rewrite,
)
from app.datascale.optimize.profiler import QueryProfiler
from app.datascale.optimize.regression import (
    BaselineStore,
    PlanRegressionGuard,
    snapshot_from_plan,
)
from app.db.inspect import explain, explain_analyze

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="KINORA_TEST_DATABASE_URL not set; skipping qopt DB integration tests",
)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    """A connection on its own engine with scratch tables created + torn down."""
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as c:
        await c.execute(text("DROP TABLE IF EXISTS qopt_shot CASCADE"))
        await c.execute(
            text(
                """
                CREATE TABLE qopt_shot (
                    id bigserial PRIMARY KEY,
                    book_id bigint NOT NULL,
                    page_no int NOT NULL,
                    status text NOT NULL DEFAULT 'pending'
                )
                """
            )
        )
        # Seed enough rows that a seq scan is "expensive" and an index matters.
        await c.execute(
            text(
                """
                INSERT INTO qopt_shot (book_id, page_no, status)
                SELECT (g % 100) + 1, g % 300, 'rendered'
                FROM generate_series(1, 20000) AS g
                """
            )
        )
        await c.execute(text("ANALYZE qopt_shot"))
    try:
        async with engine.connect() as c:
            yield c
    finally:
        async with engine.begin() as c:
            await c.execute(text("DROP TABLE IF EXISTS qopt_shot CASCADE"))
        await engine.dispose()


# --------------------------------------------------------------------------- #
# EXPLAIN inspector -> profiler / regression guard
# --------------------------------------------------------------------------- #


async def test_explain_feeds_profiler(conn: AsyncConnection) -> None:
    plan = await explain(conn, "SELECT * FROM qopt_shot WHERE book_id = 1")
    prof = QueryProfiler()
    prof.record_plan("SELECT * FROM qopt_shot WHERE book_id = 1", plan)
    report = prof.report()
    assert len(report.shapes) == 1
    assert report.shapes[0].mean_plan_cost > 0


async def test_seq_scan_detected_then_index_fixes_it(conn: AsyncConnection) -> None:
    sql = "SELECT * FROM qopt_shot WHERE book_id = 7"
    # Without an index, a filter on book_id is a seq scan over 20k rows.
    before = await explain(conn, sql)
    assert before.used_seq_scan

    # Build the index the advisor would recommend, then re-explain.
    await conn.execute(
        text('CREATE INDEX ix_qopt_shot_book_id ON qopt_shot ("book_id")')
    )
    await conn.execute(text("ANALYZE qopt_shot"))
    after = await explain(conn, sql)
    assert not after.used_seq_scan
    assert after.total_cost < before.total_cost


async def test_regression_guard_against_real_plans(conn: AsyncConnection) -> None:
    sql = "SELECT * FROM qopt_shot WHERE book_id = 3"
    # Capture a GOOD baseline (with an index).
    await conn.execute(text('CREATE INDEX ix_qopt_shot_bid ON qopt_shot ("book_id")'))
    await conn.execute(text("ANALYZE qopt_shot"))
    good_plan = await explain(conn, sql)
    baseline = snapshot_from_plan(sql, good_plan)
    store = BaselineStore()
    store.put(baseline)
    guard = PlanRegressionGuard(store)

    # Drop the index → the same query regresses to a seq scan.
    await conn.execute(text("DROP INDEX ix_qopt_shot_bid"))
    await conn.execute(text("ANALYZE qopt_shot"))
    bad_plan = await explain(conn, sql)
    diff = guard.check(snapshot_from_plan(sql, bad_plan))
    assert diff is not None
    assert diff.regressed
    assert diff.new_seq_scan


# --------------------------------------------------------------------------- #
# Matview executor: real DDL + refresh
# --------------------------------------------------------------------------- #


async def test_matview_create_and_full_refresh(conn: AsyncConnection) -> None:
    reg = MatviewRegistry()
    clock = StalenessClock()
    mv = MatviewDef(
        name="qopt_mv_counts",
        select_sql="SELECT book_id, count(*) AS n FROM qopt_shot GROUP BY book_id",
    )
    reg.register(mv)
    executor = MatviewExecutor(reg, clock)
    try:
        await executor.create(conn, "qopt_mv_counts")
        # The MV materialised one row per book.
        n_rows = (await conn.execute(text("SELECT count(*) FROM qopt_mv_counts"))).scalar_one()
        assert n_rows == 100
        assert not clock.is_stale(mv)  # freshly refreshed

        # Insert new rows; the MV is stale until refreshed.
        await conn.execute(
            text("INSERT INTO qopt_shot (book_id, page_no) VALUES (1, 999)")
        )
        n_before = (
            await conn.execute(
                text("SELECT n FROM qopt_mv_counts WHERE book_id = 1")
            )
        ).scalar_one()
        await executor.refresh_full(conn, "qopt_mv_counts")
        n_after = (
            await conn.execute(
                text("SELECT n FROM qopt_mv_counts WHERE book_id = 1")
            )
        ).scalar_one()
        assert n_after == n_before + 1
    finally:
        await executor.drop(conn, "qopt_mv_counts")


async def test_matview_rewrite_returns_equivalent_rows(conn: AsyncConnection) -> None:
    # Prove the rewrite is not just sound on paper: the rewritten SQL against the
    # MV returns the same answer as the original against the base table.
    reg = MatviewRegistry()
    clock = StalenessClock()
    mv = MatviewDef(
        name="qopt_mv_counts2",
        select_sql="SELECT book_id, count(*) FROM qopt_shot GROUP BY book_id",
    )
    reg.register(mv)
    executor = MatviewExecutor(reg, clock)
    try:
        await executor.create(conn, "qopt_mv_counts2")
        original_sql = (
            "SELECT book_id, count(*) FROM qopt_shot WHERE book_id = 5 GROUP BY book_id"
        )
        original = (await conn.execute(text(original_sql))).all()
        result = rewrite(original_sql, reg)
        assert result is not None
        # The rewritten SQL has a parameter placeholder for book_id = 5.
        rewritten_sql = result.sql.replace("?", "5")
        rewritten = (await conn.execute(text(rewritten_sql))).all()
        assert [tuple(r) for r in rewritten] == [tuple(r) for r in original]
    finally:
        await executor.drop(conn, "qopt_mv_counts2")


async def test_incremental_refresh_plan_applies(conn: AsyncConnection) -> None:
    # Model an incremental matview as a plain table maintained by delete+insert.
    reg = MatviewRegistry()
    clock = StalenessClock()
    mv = MatviewDef(
        name="qopt_inc_counts",
        select_sql="SELECT book_id, count(*) AS n FROM qopt_shot GROUP BY book_id",
        freshness=FreshnessPolicy(incremental_key="book_id"),
    )
    reg.register(mv)
    try:
        # Build the "materialized table" by hand (incremental MVs are tables).
        await conn.execute(
            text(
                "CREATE TABLE qopt_inc_counts AS "
                "SELECT book_id, count(*) AS n FROM qopt_shot GROUP BY book_id"
            )
        )
        clock.mark_refreshed("qopt_inc_counts")

        # A write to book 1 dirties it; the planner scopes the refresh to book 1.
        await conn.execute(text("INSERT INTO qopt_shot (book_id, page_no) VALUES (1, 1)"))
        clock.mark_dirty("qopt_inc_counts", 1)
        plan = RefreshPlanner(reg, clock).plan_incremental("qopt_inc_counts")
        assert plan.kind == "incremental"
        # Apply the planned delete+insert, binding the dirty key.
        params = {f"k{i}": v for i, v in enumerate(plan.key_values)}
        for stmt in plan.sql:
            await conn.execute(text(stmt), params)
        n = (
            await conn.execute(
                text("SELECT n FROM qopt_inc_counts WHERE book_id = 1")
            )
        ).scalar_one()
        # Book 1 originally had 200 rows (20000/100) + 1 inserted = 201.
        assert n == 201
    finally:
        await conn.execute(text("DROP TABLE IF EXISTS qopt_inc_counts"))


async def test_explain_analyze_records_execution_time(conn: AsyncConnection) -> None:
    plan = await explain_analyze(conn, "SELECT count(*) FROM qopt_shot WHERE book_id = 2")
    assert plan.execution_time_ms is not None
    prof = QueryProfiler()
    prof.record_plan("SELECT count(*) FROM qopt_shot WHERE book_id = 2", plan)
    assert prof.report().shapes[0].total_ms >= 0
