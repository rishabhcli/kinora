"""Unit tests for the Z-set delta algebra (no infra)."""

from __future__ import annotations

import pytest

from app.streaming.cdc.views.delta import (
    Row,
    ZSet,
    delete_delta,
    freeze,
    insert_delta,
    update_delta,
)


def r(**cells: object) -> Row:
    return Row(cells)


def test_row_is_hashable_and_order_independent() -> None:
    assert r(a=1, b=2) == r(b=2, a=1)
    assert hash(r(a=1, b=2)) == hash(r(b=2, a=1))
    assert r(a=1, b=2).as_dict() == {"a": 1, "b": 2}
    assert r(a=1).get("a") == 1
    assert r(a=1).get("missing", "d") == "d"


def test_freeze_makes_nested_structures_hashable() -> None:
    row = r(tags=["x", "y"], meta={"k": [1, 2]})
    assert hash(row)  # does not raise
    assert row.get("tags") == ("x", "y")
    assert freeze({"a": [1, {"b": 2}]}) == (("a", (1, (("b", 2),))),)


def test_zset_addition_merges_weights() -> None:
    z = ZSet.singleton(r(id=1)) + ZSet.singleton(r(id=1))
    assert z.weight(r(id=1)) == 2
    assert z.rows() == [r(id=1)]


def test_zset_retraction_cancels_to_zero_and_prunes() -> None:
    z = ZSet.singleton(r(id=1), 1)
    z += ZSet.singleton(r(id=1), -1)
    assert len(z) == 0
    assert z.rows() == []
    assert z.is_consistent()


def test_negative_weight_is_inconsistent() -> None:
    z = delete_delta(r(id=1))
    assert not z.is_consistent()
    assert z.weight(r(id=1)) == -1


def test_update_delta_retracts_old_asserts_new() -> None:
    d = update_delta(r(id=1, v="a"), r(id=1, v="b"))
    assert d.weight(r(id=1, v="a")) == -1
    assert d.weight(r(id=1, v="b")) == 1


def test_update_delta_handles_none_sides() -> None:
    assert insert_delta(r(id=1)) == update_delta(None, r(id=1))
    assert delete_delta(r(id=1)) == update_delta(r(id=1), None)


def test_apply_sequence_is_associative() -> None:
    base = ZSet()
    deltas = [
        insert_delta(r(id=1)),
        insert_delta(r(id=2)),
        update_delta(r(id=1), r(id=1, v=2)),
        delete_delta(r(id=2)),
    ]
    for d in deltas:
        base += d
    assert base.is_consistent()
    assert base.rows() == [r(id=1, v=2)]


def test_filter_preserves_weights() -> None:
    z = ZSet.from_rows([r(id=1, ok=True), r(id=2, ok=False)])
    kept = z.filter(lambda row: row.get("ok") is True)
    assert kept.rows() == [r(id=1, ok=True)]


def test_zset_unhashable() -> None:
    with pytest.raises(TypeError):
        hash(ZSet())
