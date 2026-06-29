"""Unit tests for the hot-path profiler + flamegraph folding (no infra)."""

from __future__ import annotations

from app.datascale.optimize.profiler import (
    FlameGraph,
    QueryProfiler,
    ShapeStat,
)

# --------------------------------------------------------------------------- #
# ShapeStat
# --------------------------------------------------------------------------- #


def test_shape_stat_aggregates() -> None:
    s = ShapeStat(fingerprint="abc", skeleton="select * from t where id = ?")
    s.observe(10.0, rows=1)
    s.observe(20.0, rows=3)
    s.observe(30.0, rows=2)
    assert s.calls == 3
    assert s.total_ms == 60.0
    assert s.mean_ms == 20.0
    assert s.max_ms == 30.0
    assert s.min_ms == 10.0
    assert s.mean_rows == 2.0


def test_shape_stat_percentile() -> None:
    s = ShapeStat(fingerprint="abc", skeleton="q")
    for d in range(1, 101):  # 1..100 ms
        s.observe(float(d))
    assert s.percentile(50) == 50.0
    assert s.percentile(95) == 95.0
    assert s.percentile(100) == 100.0
    assert s.percentile(0) == 1.0


def test_shape_stat_seq_scan_flag() -> None:
    s = ShapeStat(fingerprint="abc", skeleton="q")
    assert not s.uses_seq_scan
    s.observe_plan(100.0, used_seq_scan=False)
    assert not s.uses_seq_scan
    s.observe_plan(5000.0, used_seq_scan=True)
    assert s.uses_seq_scan
    assert s.mean_plan_cost == 2550.0


# --------------------------------------------------------------------------- #
# FlameGraph
# --------------------------------------------------------------------------- #


def test_flamegraph_folds_stacks() -> None:
    fg = FlameGraph()
    fg.add(["req", "BookRepo.get", "select book"], weight=10)
    fg.add(["req", "BookRepo.get", "select book"], weight=5)
    fg.add(["req", "ShotRepo.list", "select shot"], weight=3)
    folded = fg.fold()
    lines = folded.split("\n")
    assert "req;BookRepo.get;select book 15" in lines
    assert "req;ShotRepo.list;select shot 3" in lines
    assert fg.total_weight() == 18


def test_flamegraph_tree() -> None:
    fg = FlameGraph()
    fg.add(["a", "b"], weight=2)
    fg.add(["a", "c"], weight=3)
    tree = fg.tree()
    assert tree["name"] == "root"
    assert tree["value"] == 5
    a = tree["children"][0]
    assert a["name"] == "a"
    assert a["value"] == 5
    assert {c["name"] for c in a["children"]} == {"b", "c"}


def test_flamegraph_empty_stack_ignored() -> None:
    fg = FlameGraph()
    fg.add([], weight=5)
    assert fg.fold() == ""
    assert fg.total_weight() == 0


# --------------------------------------------------------------------------- #
# QueryProfiler
# --------------------------------------------------------------------------- #


def test_profiler_groups_by_shape() -> None:
    prof = QueryProfiler()
    # Same shape, different literals -> one shape.
    prof.record("SELECT * FROM book WHERE id = 1", 5.0, rows=1)
    prof.record("SELECT * FROM book WHERE id = 2", 7.0, rows=1)
    prof.record("SELECT * FROM shot WHERE book_id = 1", 50.0, rows=100)
    report = prof.report()
    assert len(report.shapes) == 2
    # Hottest by total time first.
    assert "shot" in report.shapes[0].skeleton
    assert report.shapes[1].calls == 2
    assert report.total_calls == 3
    assert report.total_ms == 62.0


def test_profiler_top_n() -> None:
    prof = QueryProfiler()
    prof.record("SELECT 1 FROM a", 1.0)
    prof.record("SELECT 1 FROM b", 100.0)
    prof.record("SELECT 1 FROM c", 50.0)
    top = prof.report().top(2)
    assert [s.skeleton.split()[-1] for s in top] == ["b", "c"]


def test_profiler_record_plan_seq_scan() -> None:
    from app.db.inspect import PlanNode, QueryPlan

    root = PlanNode(
        node_type="Seq Scan",
        relation="shot",
        total_cost=9999.0,
        plan_rows=100000,
        actual_rows=100000,
    )
    plan = QueryPlan(
        root=root,
        total_cost=9999.0,
        execution_time_ms=250.0,
        planning_time_ms=1.0,
        risks=["Seq Scan on shot ..."],
        raw={},
    )
    prof = QueryProfiler()
    prof.record_plan("SELECT * FROM shot WHERE status = 'x'", plan)
    report = prof.report()
    assert report.seq_scan_offenders()
    s = report.shapes[0]
    assert s.mean_plan_cost == 9999.0
    assert s.uses_seq_scan
    # execution_time was also recorded as a duration.
    assert s.total_ms == 250.0


def test_profiler_ingest_slow_queries() -> None:
    from app.db.engine import SlowQueryRecord

    records = [
        SlowQueryRecord(statement="SELECT * FROM shot WHERE book_id = 1", duration_ms=600.0,
                        rowcount=12),
        SlowQueryRecord(statement="SELECT * FROM shot WHERE book_id = 2", duration_ms=700.0,
                        rowcount=8),
    ]
    prof = QueryProfiler()
    n = prof.ingest_slow_queries(records)
    assert n == 2
    report = prof.report()
    # Both collapse to one shape.
    assert len(report.shapes) == 1
    assert report.shapes[0].calls == 2
    assert report.shapes[0].rows_total == 20


def test_profiler_flamegraph_from_record() -> None:
    prof = QueryProfiler()
    prof.record(
        "SELECT * FROM book WHERE id = 1",
        12.0,
        stack=["GET /book/{id}", "BookRepo.get"],
    )
    folded = prof.report().flamegraph_folded
    assert "GET /book/{id};BookRepo.get;select * from book where id = ? 12" in folded


def test_profiler_reset() -> None:
    prof = QueryProfiler()
    prof.record("SELECT 1 FROM t", 1.0)
    prof.reset()
    assert prof.report().total_calls == 0
    assert prof.report().flamegraph_folded == ""
