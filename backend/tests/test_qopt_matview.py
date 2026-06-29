"""Unit tests for the materialized-view system: definitions, registry, refresh,
and — most importantly — rewrite soundness (accept AND decline). No infra."""

from __future__ import annotations

import pytest

from app.datascale.optimize.errors import RewriteUnsound, UnknownMatview
from app.datascale.optimize.matview import (
    FreshnessPolicy,
    MatviewDef,
    MatviewRegistry,
    RefreshPlanner,
    StalenessClock,
    create_matview_ddl,
    drop_matview_ddl,
    rewrite,
    rewrite_strict,
    unique_index_ddl,
    with_dependencies,
)

# --------------------------------------------------------------------------- #
# Definition + registry
# --------------------------------------------------------------------------- #


def test_def_derives_dependencies_and_columns() -> None:
    mv = MatviewDef(
        name="mv_shots_per_book",
        select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
    )
    assert mv.dependencies == frozenset({"shot"})
    assert "book_id" in mv.materialized_columns


def test_dependency_override() -> None:
    mv = with_dependencies(
        MatviewDef(name="mv_x", select_sql="SELECT id FROM shot"),
        ["shot", "book"],
    )
    assert mv.dependencies == frozenset({"shot", "book"})


def test_def_with_bad_select_raises_at_construction() -> None:
    from app.datascale.optimize.errors import ParseError

    with pytest.raises(ParseError):
        MatviewDef(name="bad", select_sql="UPDATE shot SET x = 1")


def test_registry_register_get_unregister() -> None:
    reg = MatviewRegistry()
    mv = MatviewDef(name="mv1", select_sql="SELECT id FROM shot")
    reg.register(mv)
    assert "mv1" in reg
    assert reg.get("mv1") is mv
    assert reg.views_for_table("shot") == [mv]
    reg.unregister("mv1")
    assert "mv1" not in reg
    assert reg.views_for_table("shot") == []


def test_registry_unknown_raises() -> None:
    reg = MatviewRegistry()
    with pytest.raises(UnknownMatview):
        reg.get("nope")


def test_registry_candidates_share_table() -> None:
    reg = MatviewRegistry()
    reg.register(MatviewDef(name="mv_shot", select_sql="SELECT id FROM shot"))
    reg.register(MatviewDef(name="mv_book", select_sql="SELECT id FROM book"))
    from app.datascale.optimize.sqlshape import parse_select

    cands = reg.candidates_for(parse_select("SELECT id FROM shot WHERE id = 1"))
    assert [c.name for c in cands] == ["mv_shot"]


# --------------------------------------------------------------------------- #
# Rewrite — ACCEPT cases (must rewrite)
# --------------------------------------------------------------------------- #


def _reg(mv: MatviewDef) -> MatviewRegistry:
    reg = MatviewRegistry()
    reg.register(mv)
    return reg


def test_rewrite_identical_projection() -> None:
    reg = _reg(MatviewDef(name="mv_a", select_sql="SELECT id, title FROM book"))
    res = rewrite("SELECT id, title FROM book", reg)
    assert res is not None
    assert res.matview == "mv_a"
    assert '"mv_a"' in res.sql


def test_rewrite_subset_columns() -> None:
    reg = _reg(MatviewDef(name="mv_b", select_sql="SELECT id, title, author FROM book"))
    res = rewrite("SELECT id FROM book", reg)
    assert res is not None
    assert res.matview == "mv_b"


def test_rewrite_aggregate_with_group_key_equality() -> None:
    # MV pre-aggregates count per book; query asks count for one book.
    reg = _reg(
        MatviewDef(
            name="mv_counts",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
        )
    )
    res = rewrite("SELECT book_id, count(*) FROM shot WHERE book_id = 7 GROUP BY book_id", reg)
    assert res is not None
    assert res.matview == "mv_counts"
    assert "where" in res.sql.lower()


def test_rewrite_strict_accepts() -> None:
    reg = _reg(MatviewDef(name="mv_s", select_sql="SELECT id FROM book"))
    res = rewrite_strict("SELECT id FROM book", reg)
    assert res.matview == "mv_s"


def test_rewrite_preserves_order_and_limit() -> None:
    reg = _reg(MatviewDef(name="mv_o", select_sql="SELECT id, created_at FROM shot"))
    res = rewrite("SELECT id FROM shot ORDER BY created_at LIMIT 10", reg)
    assert res is not None
    assert "order by" in res.sql.lower()
    assert "limit 10" in res.sql.lower()


# --------------------------------------------------------------------------- #
# Rewrite — DECLINE cases (where correctness lives — must NOT rewrite)
# --------------------------------------------------------------------------- #


def test_decline_different_table() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    assert rewrite("SELECT id FROM book", reg) is None


def test_decline_column_not_materialised() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM book"))
    # 'title' is not in the MV.
    assert rewrite("SELECT id, title FROM book", reg) is None


def test_decline_mv_more_restrictive() -> None:
    # The MV only stores rendered shots; a query over all shots cannot use it.
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot WHERE status = 'rendered'"))
    assert rewrite("SELECT id FROM shot", reg) is None


def test_decline_aggregate_function_mismatch() -> None:
    # MV stores COUNT; query wants AVG — cannot derive AVG from COUNT.
    reg = _reg(
        MatviewDef(name="mv", select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id")
    )
    assert rewrite("SELECT book_id, avg(duration) FROM shot GROUP BY book_id", reg) is None


def test_decline_extra_predicate_not_on_group_key() -> None:
    # MV groups by book_id; query filters on status (not a group key) → unsound.
    reg = _reg(
        MatviewDef(name="mv", select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id")
    )
    assert (
        rewrite(
            "SELECT book_id, count(*) FROM shot WHERE status = 'x' GROUP BY book_id", reg
        )
        is None
    )


def test_decline_unshapable_query() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    # A query the shape parser rejects → decline, do not raise.
    assert rewrite("SELECT id FROM a UNION SELECT id FROM b", reg) is None


def test_rewrite_strict_raises_on_decline() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    with pytest.raises(RewriteUnsound):
        rewrite_strict("SELECT id FROM book", reg)


def test_decline_when_registry_empty() -> None:
    assert rewrite("SELECT id FROM shot", MatviewRegistry()) is None


# --------------------------------------------------------------------------- #
# Staleness clock
# --------------------------------------------------------------------------- #


def test_staleness_clock_age_and_stale() -> None:
    t = [1000.0]
    clock = StalenessClock(now=lambda: t[0])
    mv = MatviewDef(name="mv", select_sql="SELECT id FROM shot",
                    freshness=FreshnessPolicy(max_staleness_s=60))
    # Never refreshed → stale.
    assert clock.is_stale(mv)
    assert clock.age_s("mv") is None
    clock.mark_refreshed("mv")
    assert clock.age_s("mv") == 0.0
    assert not clock.is_stale(mv)
    t[0] += 61
    assert clock.age_s("mv") == 61
    assert clock.is_stale(mv)


def test_dirty_keys_make_stale() -> None:
    clock = StalenessClock(now=lambda: 1.0)
    mv = MatviewDef(
        name="mv",
        select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
        freshness=FreshnessPolicy(max_staleness_s=999, incremental_key="book_id"),
    )
    clock.mark_refreshed("mv")
    assert not clock.is_stale(mv)
    clock.mark_dirty("mv", "book-7")
    assert clock.is_stale(mv)
    assert clock.dirty_keys("mv") == frozenset({"book-7"})
    clock.mark_refreshed("mv")
    assert clock.dirty_keys("mv") == frozenset()


# --------------------------------------------------------------------------- #
# Refresh planning
# --------------------------------------------------------------------------- #


def test_plan_full() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    planner = RefreshPlanner(reg, StalenessClock())
    plan = planner.plan_full("mv")
    assert plan.kind == "full"
    assert plan.sql == ('REFRESH MATERIALIZED VIEW "mv"',)


def test_plan_full_concurrently() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    planner = RefreshPlanner(reg, StalenessClock())
    plan = planner.plan_full("mv", concurrently=True)
    assert "CONCURRENTLY" in plan.sql[0]


def test_plan_incremental_skips_when_clean() -> None:
    reg = _reg(
        MatviewDef(
            name="mv",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
            freshness=FreshnessPolicy(incremental_key="book_id"),
        )
    )
    planner = RefreshPlanner(reg, StalenessClock())
    plan = planner.plan_incremental("mv")
    assert plan.is_noop


def test_plan_incremental_scopes_to_dirty_keys() -> None:
    clock = StalenessClock()
    reg = _reg(
        MatviewDef(
            name="mv",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
            freshness=FreshnessPolicy(incremental_key="book_id"),
        )
    )
    planner = RefreshPlanner(reg, clock)
    clock.mark_dirty("mv", "b1")
    clock.mark_dirty("mv", "b2")
    plan = planner.plan_incremental("mv")
    assert plan.kind == "incremental"
    assert len(plan.sql) == 2  # delete + insert
    assert "DELETE FROM" in plan.sql[0]
    assert "INSERT INTO" in plan.sql[1]
    assert set(plan.key_values) == {"b1", "b2"}


def test_plan_incremental_falls_back_to_full_without_key() -> None:
    reg = _reg(MatviewDef(name="mv", select_sql="SELECT id FROM shot"))
    planner = RefreshPlanner(reg, StalenessClock())
    plan = planner.plan_incremental("mv")
    assert plan.kind == "full"


def test_plan_for_writes_marks_and_plans() -> None:
    clock = StalenessClock()
    reg = MatviewRegistry()
    reg.register(
        MatviewDef(
            name="mv_counts",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
            freshness=FreshnessPolicy(incremental_key="book_id"),
        )
    )
    reg.register(MatviewDef(name="mv_books", select_sql="SELECT id FROM book"))
    planner = RefreshPlanner(reg, clock)
    plans = planner.plan_for_writes({"shot": ["b1"], "author": ["a9"]})
    # Only mv_counts is affected (touches 'shot'); mv_books touches 'book', not changed.
    assert [p.name for p in plans] == ["mv_counts"]
    assert plans[0].kind == "incremental"
    assert plans[0].key_values == ("b1",)


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #


def test_create_and_drop_ddl() -> None:
    mv = MatviewDef(name="mv", select_sql="SELECT id FROM shot")
    ddl = create_matview_ddl(mv)
    assert ddl.startswith('CREATE MATERIALIZED VIEW "mv" AS')
    assert "WITH DATA" in ddl
    assert create_matview_ddl(mv, with_data=False).endswith("WITH NO DATA")
    assert drop_matview_ddl("mv") == 'DROP MATERIALIZED VIEW IF EXISTS "mv"'


def test_unique_index_ddl() -> None:
    mv = MatviewDef(name="mv", select_sql="SELECT book_id FROM shot GROUP BY book_id")
    ddl = unique_index_ddl(mv, ["book_id"])
    assert ddl == 'CREATE UNIQUE INDEX "mv_uidx" ON "mv" ("book_id")'
