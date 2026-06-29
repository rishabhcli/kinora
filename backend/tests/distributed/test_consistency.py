"""Tests for tunable consistency (consistency.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.consistency import (
    ConsistencyLevel,
    ReadCoordinator,
    ReplicaAnswer,
    StalenessPolicy,
    WriteCoordinator,
    freshest,
    quorum_overlaps,
)

R = [NodeId("us", "1"), NodeId("eu", "1"), NodeId("ap", "1")]
US, EU, AP = R


def ts(wall: int, node: NodeId = US) -> HybridTimestamp:
    return HybridTimestamp(wall, 0, node)


def test_required_counts_per_level() -> None:
    assert ConsistencyLevel.ONE.required(3) == 1
    assert ConsistencyLevel.QUORUM.required(3) == 2
    assert ConsistencyLevel.QUORUM.required(4) == 3
    assert ConsistencyLevel.QUORUM.required(5) == 3
    assert ConsistencyLevel.ALL.required(3) == 3


def test_required_rejects_nonpositive_replicas() -> None:
    with pytest.raises(ValueError):
        ConsistencyLevel.ONE.required(0)


def test_quorum_overlap_rule() -> None:
    quorum = ConsistencyLevel.QUORUM
    one = ConsistencyLevel.ONE
    all_ = ConsistencyLevel.ALL
    assert quorum_overlaps(quorum, quorum, 3)  # 2 + 2 > 3
    assert quorum_overlaps(one, all_, 3)  # 1 + 3 > 3
    assert quorum_overlaps(all_, one, 3)
    assert not quorum_overlaps(one, one, 3)  # 1 + 1 !> 3
    assert not quorum_overlaps(one, quorum, 3)  # 1 + 2 !> 3


def test_write_coordinator_satisfies_at_quorum() -> None:
    wc = WriteCoordinator(ConsistencyLevel.QUORUM, R)
    assert wc.required == 2
    assert not wc.satisfied
    wc.ack(US)
    assert not wc.satisfied
    wc.ack(EU)
    assert wc.satisfied
    out = wc.outcome()
    assert out.satisfied and out.acks == 2 and out.acked_by == {US, EU}


def test_write_coordinator_ignores_unknown_and_duplicate_acks() -> None:
    wc = WriteCoordinator(ConsistencyLevel.ALL, R)
    wc.ack(US)
    wc.ack(US)  # duplicate
    wc.ack(NodeId("zz", "9"))  # not a replica
    assert not wc.satisfied
    wc.ack(EU)
    wc.ack(AP)
    assert wc.satisfied


def test_write_coordinator_requires_replicas() -> None:
    with pytest.raises(ValueError):
        WriteCoordinator(ConsistencyLevel.ONE, [])


def test_read_coordinator_returns_newest_among_quorum() -> None:
    rc: ReadCoordinator[str] = ReadCoordinator(ConsistencyLevel.QUORUM, R)
    rc.answer(ReplicaAnswer(US, "old", ts(10, US)))
    rc.answer(ReplicaAnswer(EU, "new", ts(20, EU)))
    assert rc.satisfied
    res = rc.result()
    assert res.value == "new"
    assert res.satisfied
    assert res.answers == 2


def test_read_coordinator_absent_key() -> None:
    rc: ReadCoordinator[str] = ReadCoordinator(ConsistencyLevel.ONE, R)
    rc.answer(ReplicaAnswer(US, None, None, present=False))
    res = rc.result()
    assert not res.present
    assert res.value is None
    assert res.satisfied  # ONE answer is enough


def test_read_coordinator_not_yet_satisfied() -> None:
    rc: ReadCoordinator[str] = ReadCoordinator(ConsistencyLevel.ALL, R)
    rc.answer(ReplicaAnswer(US, "v", ts(5)))
    assert not rc.satisfied
    assert not rc.result().satisfied


def test_staleness_policy_age_and_bound() -> None:
    rc: ReadCoordinator[str] = ReadCoordinator(ConsistencyLevel.ONE, R)
    rc.answer(ReplicaAnswer(US, "v", ts(1000)))
    res = rc.result()
    policy = StalenessPolicy(max_age_ms=500)
    assert policy.age_of(res, now_ms=1200) == 200
    assert policy.within_bound(res, now_ms=1200)
    assert not policy.within_bound(res, now_ms=2000)  # 1000ms old > 500 bound


def test_staleness_absent_read_has_no_age() -> None:
    rc: ReadCoordinator[str] = ReadCoordinator(ConsistencyLevel.ONE, R)
    rc.answer(ReplicaAnswer(US, None, None, present=False))
    res = rc.result()
    policy = StalenessPolicy(max_age_ms=10)
    assert policy.age_of(res, now_ms=9999) is None
    assert policy.within_bound(res, now_ms=9999)


def test_freshest_picks_highest_timestamp() -> None:
    assert freshest({US: ts(10), EU: ts(30), AP: ts(20)}) == EU
    assert freshest({}) is None
