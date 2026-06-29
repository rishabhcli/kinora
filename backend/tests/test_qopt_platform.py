"""Unit tests for the OptimizePlatform facade (no infra)."""

from __future__ import annotations

from app.datascale.optimize.matview import MatviewDef
from app.datascale.optimize.platform import OptimizePlatform
from app.datascale.optimize.resultcache import RowScope
from app.datascale.optimize.workloadgen import WorkloadGenerator, WorkloadSpec


def test_observe_records_profiler_and_detector() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform(n_plus_one_threshold=3)
    for i in range(5):
        plat.observe(
            "SELECT * FROM shot WHERE book_id = $1", latency_ms=2.0, rows=10, params=i
        )
    assert plat.hot_paths().total_calls == 5
    assert plat.n_plus_one_findings()  # the burst is detected


def test_observe_caches_and_hits() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    r1 = plat.observe(
        "SELECT * FROM book WHERE id = $1",
        params={"id": 1},
        result="rows",
        dependencies=["book"],
        cacheable=True,
    )
    assert not r1.cache_hit
    r2 = plat.observe(
        "SELECT * FROM book WHERE id = $1", params={"id": 1}, cacheable=True
    )
    assert r2.cache_hit
    assert r2.cached == "rows"


def test_on_write_invalidates_cache() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    plat.observe(
        "SELECT * FROM book WHERE id = $1",
        params={"id": 1},
        result="rows",
        dependencies=["book"],
        cacheable=True,
    )
    plat.on_write("book")
    r = plat.observe("SELECT * FROM book WHERE id = $1", params={"id": 1}, cacheable=True)
    assert not r.cache_hit


def test_on_write_row_scoped_keeps_other_rows() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    for book in (7, 9):
        plat.observe(
            "SELECT * FROM shot WHERE book_id = $1",
            params={"id": book},
            result=f"book{book}",
            dependencies=["shot"],
            row_scopes=[RowScope("shot", "book_id", book)],
            cacheable=True,
        )
    plat.on_write("shot", row_scopes=[RowScope("shot", "book_id", 7)])
    r7 = plat.observe("SELECT * FROM shot WHERE book_id = $1", params={"id": 7}, cacheable=True)
    r9 = plat.observe("SELECT * FROM shot WHERE book_id = $1", params={"id": 9}, cacheable=True)
    assert not r7.cache_hit
    assert r9.cache_hit


def test_matview_rewrite_reported() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    plat.register_matview(
        MatviewDef(
            name="mv_counts",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
        )
    )
    res = plat.observe(
        "SELECT book_id, count(*) FROM shot WHERE book_id = 7 GROUP BY book_id",
        latency_ms=20.0,
    )
    assert res.rewrite is not None
    assert res.rewrite.matview == "mv_counts"


def test_on_write_plans_matview_refresh() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    from app.datascale.optimize.matview import FreshnessPolicy

    plat.register_matview(
        MatviewDef(
            name="mv_counts",
            select_sql="SELECT book_id, count(*) FROM shot GROUP BY book_id",
            freshness=FreshnessPolicy(incremental_key="book_id"),
        )
    )
    plans = plat.on_write("shot", changed_keys=["b1"])
    assert [p.name for p in plans] == ["mv_counts"]
    assert plans[0].kind == "incremental"


def test_recommend_indexes_from_explicit_workload() -> None:
    g = WorkloadGenerator(seed=11, spec=WorkloadSpec(n_queries=1500))
    plat: OptimizePlatform[str] = OptimizePlatform(table_sizes=g.table_sizes())
    recs = plat.recommend_indexes(g.workload())
    assert recs
    assert any(r["table"] == "shot" for r in recs)


def test_recommend_indexes_from_profiled_shapes() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform(table_sizes={"shot": 1_000_000})
    for i in range(100):
        plat.observe("SELECT * FROM shot WHERE book_id = $1", latency_ms=5.0, params=i)
    recs = plat.recommend_indexes()
    assert any(
        r["table"] == "shot" and "book_id" in list(r["columns"])  # type: ignore[call-overload]
        for r in recs
    )


def test_begin_window_resets_detector() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform(n_plus_one_threshold=3)
    for i in range(5):
        plat.observe("SELECT * FROM shot WHERE id = $1", params=i)
    assert plat.n_plus_one_findings()
    plat.begin_window()
    assert plat.n_plus_one_findings() == []


def test_snapshot_stats_shape() -> None:
    plat: OptimizePlatform[str] = OptimizePlatform()
    plat.observe(
        "SELECT * FROM book WHERE id = $1",
        params={"id": 1},
        result="r",
        dependencies=["book"],
        cacheable=True,
        latency_ms=3.0,
    )
    plat.observe("SELECT * FROM book WHERE id = $1", params={"id": 1}, cacheable=True)
    stats = plat.snapshot_stats()
    assert "cache" in stats
    assert "hot_paths" in stats
    assert stats["cache"]["hits"] == 1


def test_full_loop_workload_to_recommendations() -> None:
    # End-to-end: generate a workload, drive observe(), get hot paths + indexes.
    g = WorkloadGenerator(seed=99, spec=WorkloadSpec(n_queries=1000))
    plat: OptimizePlatform[str] = OptimizePlatform(table_sizes=g.table_sizes())
    for q in g.stream():
        plat.observe(q.sql, latency_ms=q.latency_ms, rows=5, params=q.params)
    report = plat.hot_paths()
    assert report.total_calls == 1000
    assert plat.recommend_indexes()  # advisable indexes emerge from the profiled mix
