"""Tests for Merkle reconciliation (merkle.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.merkle import (
    bucket_of,
    build_merkle,
    diff_buckets,
)


def test_identical_maps_have_equal_root() -> None:
    a = build_merkle({"k1": "f1", "k2": "f2", "k3": "f3"})
    b = build_merkle({"k3": "f3", "k1": "f1", "k2": "f2"})  # different insert order
    assert a.root_hash == b.root_hash
    assert diff_buckets(a, b) == frozenset()


def test_empty_maps_match() -> None:
    assert build_merkle({}).root_hash == build_merkle({}).root_hash


def test_single_differing_value_isolates_one_bucket() -> None:
    base = {f"key{i}": f"v{i}" for i in range(50)}
    a = build_merkle(base)
    changed = dict(base)
    changed["key7"] = "DIFFERENT"
    b = build_merkle(changed)
    diffs = diff_buckets(a, b)
    assert len(diffs) == 1
    expected = bucket_of("key7", a.arity, a.depth)
    assert expected in diffs


def test_added_key_shows_as_diff() -> None:
    a = build_merkle({"x": "1"})
    b = build_merkle({"x": "1", "y": "2"})
    diffs = diff_buckets(a, b)
    assert bucket_of("y", a.arity, a.depth) in diffs


def test_diff_is_symmetric() -> None:
    a = build_merkle({"x": "1", "y": "2"})
    b = build_merkle({"x": "1", "y": "CHANGED"})
    assert diff_buckets(a, b) == diff_buckets(b, a)


def test_diff_requires_matching_shape() -> None:
    a = build_merkle({"x": "1"}, arity=4, depth=2)
    b = build_merkle({"x": "1"}, arity=16, depth=4)
    with pytest.raises(ValueError):
        diff_buckets(a, b)


def test_bucket_assignment_is_stable_and_in_range() -> None:
    for i in range(100):
        idx = bucket_of(f"key{i}", 16, 4)
        assert 0 <= idx < 16**4
        assert idx == bucket_of(f"key{i}", 16, 4)  # deterministic


def test_many_changes_isolate_their_buckets_only() -> None:
    base = {f"k{i}": f"v{i}" for i in range(200)}
    a = build_merkle(base)
    changed = dict(base)
    touched = ["k10", "k50", "k150"]
    for k in touched:
        changed[k] = "x"
    b = build_merkle(changed)
    diffs = diff_buckets(a, b)
    expected = {bucket_of(k, a.arity, a.depth) for k in touched}
    assert expected <= diffs
    # divergence is bounded: far fewer than the 200 keys' worth of buckets.
    assert len(diffs) <= len(touched)
