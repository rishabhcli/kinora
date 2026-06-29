"""Tests for version vectors (version.py)."""

from __future__ import annotations

from app.distributed.replication.clock import NodeId
from app.distributed.replication.version import VersionVector, join_all

A = NodeId("us", "a")
B = NodeId("eu", "b")
C = NodeId("ap", "c")


def test_empty_reads_zero_everywhere() -> None:
    vv = VersionVector.empty()
    assert vv.get(A) == 0
    assert vv.nodes() == frozenset()


def test_of_drops_zero_entries() -> None:
    vv = VersionVector.of({A: 3, B: 0})
    assert vv.nodes() == {A}


def test_advanced_takes_max_and_is_immutable() -> None:
    vv = VersionVector.of({A: 3})
    assert vv.advanced(A, 2) is vv  # no regression
    bumped = vv.advanced(A, 5)
    assert bumped.get(A) == 5
    assert vv.get(A) == 3  # original untouched


def test_includes_is_inclusive() -> None:
    vv = VersionVector.of({A: 3})
    assert vv.includes(A, 3)
    assert vv.includes(A, 1)
    assert not vv.includes(A, 4)


def test_dominance_partial_order() -> None:
    bigger = VersionVector.of({A: 3, B: 2})
    smaller = VersionVector.of({A: 2, B: 2})
    assert bigger.dominates(smaller)
    assert bigger.strictly_dominates(smaller)
    assert not smaller.dominates(bigger)


def test_concurrency_detection() -> None:
    left = VersionVector.of({A: 3, B: 1})
    right = VersionVector.of({A: 1, B: 3})
    assert left.concurrent_with(right)
    assert right.concurrent_with(left)
    assert not left.dominates(right)
    assert not right.dominates(left)


def test_merge_is_pointwise_max_and_commutative() -> None:
    left = VersionVector.of({A: 3, B: 1})
    right = VersionVector.of({A: 1, B: 3, C: 5})
    expected = VersionVector.of({A: 3, B: 3, C: 5})
    assert left.merge(right) == expected
    assert right.merge(left) == expected


def test_merge_is_idempotent_and_associative() -> None:
    a = VersionVector.of({A: 2})
    b = VersionVector.of({B: 4})
    c = VersionVector.of({C: 1, A: 5})
    assert a.merge(a) == a
    assert a.merge(b).merge(c) == a.merge(b.merge(c))


def test_missing_ranges_returns_our_cursor_per_node() -> None:
    ours = VersionVector.of({A: 2, B: 5})
    ahead = VersionVector.of({A: 7, B: 5, C: 3})
    gaps = ours.missing_ranges(ahead)
    # A: they have 7, we have 2 -> fetch after 2; C: we have 0 -> after 0.
    assert gaps == {A: 2, C: 0}
    # B equal -> not present.
    assert B not in gaps


def test_equality_treats_missing_as_zero() -> None:
    assert VersionVector.of({A: 1}) == VersionVector.of({A: 1, B: 0})
    assert VersionVector.empty() == VersionVector.of({A: 0})


def test_join_all_folds_many() -> None:
    vectors = [VersionVector.of({A: i}) for i in range(5)] + [VersionVector.of({B: 9})]
    joined = join_all(vectors)
    assert joined == VersionVector.of({A: 4, B: 9})
