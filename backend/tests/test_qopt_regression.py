"""Unit tests for the plan-regression guard (no infra)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.datascale.optimize.errors import RegressionDetected
from app.datascale.optimize.regression import (
    BaselineStore,
    PlanRegressionGuard,
    PlanSnapshot,
    compare_plans,
    snapshot_from_plan,
)


def _snap(
    *,
    cost: float,
    nodes: tuple[str, ...],
    seq: bool = False,
    fp: str = "fp1",
    rels: tuple[str, ...] = ("shot",),
) -> PlanSnapshot:
    return PlanSnapshot(
        fingerprint=fp,
        skeleton="select id from shot where book_id = ?",
        total_cost=cost,
        node_types=nodes,
        relations=rels,
        used_seq_scan=seq,
    )


# --------------------------------------------------------------------------- #
# compare_plans
# --------------------------------------------------------------------------- #


def test_no_regression_when_identical() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan",))
    cur = _snap(cost=10.0, nodes=("Index Scan",))
    diff = compare_plans(base, cur)
    assert not diff.regressed
    assert diff.cost_ratio == 1.0


def test_cost_blowup_flagged() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan",))
    cur = _snap(cost=100.0, nodes=("Index Scan",))
    diff = compare_plans(base, cur, cost_tolerance=1.5)
    assert diff.regressed
    assert any("cost grew" in r for r in diff.reasons)
    assert diff.cost_ratio == 10.0


def test_cost_within_tolerance_ok() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan",))
    cur = _snap(cost=14.0, nodes=("Index Scan",))
    diff = compare_plans(base, cur, cost_tolerance=1.5)
    assert not diff.regressed


def test_new_seq_scan_flagged() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan",), seq=False)
    cur = _snap(cost=11.0, nodes=("Seq Scan",), seq=True)
    diff = compare_plans(base, cur)
    assert diff.regressed
    assert diff.new_seq_scan
    assert any("sequential scan" in r for r in diff.reasons)


def test_seq_scan_in_baseline_not_flagged_as_new() -> None:
    # Already a seq scan in both -> not a *new* one; cost within tolerance.
    base = _snap(cost=100.0, nodes=("Seq Scan",), seq=True)
    cur = _snap(cost=110.0, nodes=("Seq Scan",), seq=True)
    diff = compare_plans(base, cur)
    assert not diff.new_seq_scan
    assert not diff.regressed


def test_added_and_removed_nodes_tracked() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan", "Sort"))
    cur = _snap(cost=12.0, nodes=("Index Scan",))
    diff = compare_plans(base, cur)
    assert diff.removed_nodes == ["Sort"]
    assert diff.added_nodes == []


def test_new_regression_node_other_than_seq_scan_counts() -> None:
    base = _snap(cost=10.0, nodes=("Index Scan",), seq=False)
    # A Seq Scan node added but the snapshot's used_seq_scan flag stayed False
    # (defensive): the node-shape check still catches it.
    cur = _snap(cost=11.0, nodes=("Index Scan", "Seq Scan"), seq=False)
    diff = compare_plans(base, cur)
    assert any("Seq Scan" in r for r in diff.reasons)


# --------------------------------------------------------------------------- #
# snapshot_from_plan (adapter over the EXPLAIN inspector)
# --------------------------------------------------------------------------- #


def test_snapshot_from_query_plan() -> None:
    from app.db.inspect import PlanNode, QueryPlan

    child = PlanNode(
        node_type="Seq Scan", relation="shot", total_cost=500.0, plan_rows=10000,
        actual_rows=10000,
    )
    root = PlanNode(
        node_type="Aggregate", relation=None, total_cost=510.0, plan_rows=1,
        actual_rows=1, children=[child],
    )
    plan = QueryPlan(
        root=root, total_cost=510.0, execution_time_ms=42.0, planning_time_ms=1.0,
        risks=[], raw={},
    )
    snap = snapshot_from_plan("SELECT count(*) FROM shot WHERE status = 'x'", plan)
    assert snap.total_cost == 510.0
    assert "Seq Scan" in snap.node_types
    assert "Aggregate" in snap.node_types
    assert snap.relations == ("shot",)
    assert snap.used_seq_scan


# --------------------------------------------------------------------------- #
# BaselineStore
# --------------------------------------------------------------------------- #


def test_store_put_get() -> None:
    store = BaselineStore()
    snap = _snap(cost=10.0, nodes=("Index Scan",), fp="abc")
    store.put(snap)
    assert store.get("abc") is snap
    assert store.get("missing") is None
    assert len(store) == 1
    assert "abc" in store


def test_store_get_for_sql() -> None:
    from app.datascale.optimize.fingerprint import make_fingerprint

    sql = "SELECT id FROM shot WHERE book_id = 1"
    fp = make_fingerprint(sql).hexdigest
    store = BaselineStore()
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp=fp))
    assert store.get_for_sql("SELECT id FROM shot WHERE book_id = 999") is not None


def test_store_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.put(_snap(cost=10.0, nodes=("Index Scan", "Sort"), fp="aaa", rels=("shot",)))
    store.put(_snap(cost=20.0, nodes=("Seq Scan",), seq=True, fp="bbb", rels=("book",)))
    assert store.save() == 2

    reloaded = BaselineStore(tmp_path)
    assert reloaded.load() == 2
    a = reloaded.get("aaa")
    assert a is not None
    assert a.node_types == ("Index Scan", "Sort")
    b = reloaded.get("bbb")
    assert b is not None
    assert b.used_seq_scan


def test_store_load_missing_dir_returns_zero(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path / "does_not_exist")
    assert store.load() == 0


def test_store_save_without_dir_raises() -> None:
    store = BaselineStore()
    with pytest.raises(ValueError):
        store.save()


# --------------------------------------------------------------------------- #
# PlanRegressionGuard
# --------------------------------------------------------------------------- #


def test_guard_passes_when_no_regression() -> None:
    store = BaselineStore()
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp="fp1"))
    guard = PlanRegressionGuard(store)
    diff = guard.check(_snap(cost=11.0, nodes=("Index Scan",), fp="fp1"))
    assert diff is not None
    assert not diff.regressed
    guard.assert_no_regression(_snap(cost=11.0, nodes=("Index Scan",), fp="fp1"))


def test_guard_detects_regression() -> None:
    store = BaselineStore()
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp="fp1"))
    guard = PlanRegressionGuard(store)
    with pytest.raises(RegressionDetected) as exc:
        guard.assert_no_regression(_snap(cost=999.0, nodes=("Seq Scan",), seq=True, fp="fp1"))
    assert exc.value.diff is not None


def test_guard_missing_baseline_returns_none() -> None:
    store = BaselineStore()
    guard = PlanRegressionGuard(store)
    assert guard.check(_snap(cost=10.0, nodes=("Index Scan",), fp="unknown")) is None
    # And assert_no_regression is a no-op without a baseline (nothing to compare).
    guard.assert_no_regression(_snap(cost=10.0, nodes=("Index Scan",), fp="unknown"))


def test_guard_check_all_report() -> None:
    store = BaselineStore()
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp="good"))
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp="bad"))
    guard = PlanRegressionGuard(store)
    report = guard.check_all(
        [
            _snap(cost=11.0, nodes=("Index Scan",), fp="good"),
            _snap(cost=500.0, nodes=("Seq Scan",), seq=True, fp="bad"),
            _snap(cost=5.0, nodes=("Index Scan",), fp="no_baseline"),
        ]
    )
    assert not report.ok
    assert len(report.regressions) == 1
    assert report.regressions[0].fingerprint == "bad"
    assert report.missing_baselines == ["no_baseline"]
    assert report.as_dict()["checked"] == 2


def test_custom_cost_tolerance() -> None:
    store = BaselineStore()
    store.put(_snap(cost=10.0, nodes=("Index Scan",), fp="fp1"))
    # A lenient guard tolerates a 3× cost growth.
    guard = PlanRegressionGuard(store, cost_tolerance=5.0)
    diff = guard.check(_snap(cost=30.0, nodes=("Index Scan",), fp="fp1"))
    assert diff is not None
    assert not diff.regressed
