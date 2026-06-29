"""Assignment-strategy tests — range, round-robin, cooperative-sticky balance + stickiness."""

from __future__ import annotations

import pytest

from app.streaming.log.errors import InvalidConfigError
from app.streaming.log.group.assignor import (
    CooperativeStickyAssignor,
    RangeAssignor,
    RoundRobinAssignor,
    get_assignor,
)
from app.streaming.log.record import TopicPartition


def _tp(topic: str, *ps: int) -> tuple[TopicPartition, ...]:
    return tuple(TopicPartition(topic, p) for p in ps)


def test_range_assigns_contiguous_per_topic() -> None:
    members = {"m1": ("t",), "m2": ("t",)}
    result = RangeAssignor().assign(members, {"t": 4})
    assert result["m1"] == _tp("t", 0, 1)
    assert result["m2"] == _tp("t", 2, 3)


def test_range_handles_uneven_split() -> None:
    members = {"m1": ("t",), "m2": ("t",), "m3": ("t",)}
    result = RangeAssignor().assign(members, {"t": 5})
    # 5 / 3 → first member gets the remainder.
    assert result["m1"] == _tp("t", 0, 1)
    assert result["m2"] == _tp("t", 2, 3)
    assert result["m3"] == _tp("t", 4)


def test_round_robin_spreads_across_topics() -> None:
    members = {"m1": ("a", "b"), "m2": ("a", "b")}
    result = RoundRobinAssignor().assign(members, {"a": 2, "b": 2})
    total = sorted(result["m1"] + result["m2"])
    assert total == sorted(_tp("a", 0, 1) + _tp("b", 0, 1))
    # Even split (2 each).
    assert len(result["m1"]) == 2
    assert len(result["m2"]) == 2


def test_round_robin_respects_subscriptions() -> None:
    members = {"m1": ("a",), "m2": ("b",)}
    result = RoundRobinAssignor().assign(members, {"a": 2, "b": 2})
    assert all(tp.topic == "a" for tp in result["m1"])
    assert all(tp.topic == "b" for tp in result["m2"])


def test_cooperative_sticky_balances_from_empty() -> None:
    members = {"m1": ("t",), "m2": ("t",)}
    result = CooperativeStickyAssignor().assign(members, {"t": 4})
    assert sorted(result["m1"] + result["m2"]) == list(_tp("t", 0, 1, 2, 3))
    assert abs(len(result["m1"]) - len(result["m2"])) <= 1


def test_cooperative_sticky_preserves_ownership_on_member_join() -> None:
    assignor = CooperativeStickyAssignor()
    # m1 currently owns all 4 partitions; m2 joins.
    current = {"m1": _tp("t", 0, 1, 2, 3), "m2": ()}
    members = {"m1": ("t",), "m2": ("t",)}
    result = assignor.assign(members, {"t": 4}, current)
    # m1 keeps 2 of its originals; m2 gets the surplus — minimal movement.
    assert len(result["m1"]) == 2
    assert len(result["m2"]) == 2
    assert set(result["m1"]).issubset(set(current["m1"]))


def test_cooperative_sticky_drops_unsubscribed_partitions() -> None:
    assignor = CooperativeStickyAssignor()
    current = {"m1": _tp("old", 0)}
    members = {"m1": ("new",)}
    result = assignor.assign(members, {"new": 2, "old": 1}, current)
    # m1 no longer subscribes to "old", so it must not retain old-0.
    assert all(tp.topic == "new" for tp in result["m1"])


def test_get_assignor_by_name_and_unknown() -> None:
    assert isinstance(get_assignor("range"), RangeAssignor)
    assert isinstance(get_assignor("roundrobin"), RoundRobinAssignor)
    assert isinstance(get_assignor("cooperative-sticky"), CooperativeStickyAssignor)
    with pytest.raises(InvalidConfigError):
        get_assignor("nope")


def test_no_members_yields_empty() -> None:
    assert RoundRobinAssignor().assign({}, {"t": 4}) == {}
    assert CooperativeStickyAssignor().assign({}, {"t": 4}) == {}
