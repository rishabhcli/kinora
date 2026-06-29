"""Consumer-group tests — rebalance on join/leave, generations, fencing, heartbeats."""

from __future__ import annotations

import pytest

from app.streaming.log.broker import Broker
from app.streaming.log.consumer import Consumer, ConsumerConfig
from app.streaming.log.errors import (
    CommitConflictError,
    UnknownMemberError,
)
from app.streaming.log.group.coordinator import GroupCoordinator
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.record import TopicPartition
from app.streaming.log.redis import FakeStreamRedis, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig


@pytest.fixture(params=["memory", "redis"])
async def broker(request: pytest.FixtureRequest) -> Broker:
    impl: Broker = (
        InMemoryBroker()
        if request.param == "memory"
        else RedisStreamsBroker(FakeStreamRedis(), namespace="grp")
    )
    await impl.start()
    await impl.create_topic(TopicConfig.deleted("beats", partitions=4))
    return impl


async def test_single_member_owns_all_partitions(broker: Broker) -> None:
    result = await broker.join_group("g", member_id=None, subscription=("beats",))
    assert len(result.assignment) == 4
    assert result.is_leader


async def test_two_members_split_partitions(broker: Broker) -> None:
    r1 = await broker.join_group("g", member_id="m1", subscription=("beats",))
    r2 = await broker.join_group("g", member_id="m2", subscription=("beats",))
    # The join bumped the generation; re-join m1 to see the rebalanced split.
    r1b = await broker.join_group("g", member_id="m1", subscription=("beats",))
    all_parts = sorted(set(r1b.assignment) | set(r2.assignment))
    assert all_parts == [TopicPartition("beats", p) for p in range(4)]
    assert len(r1b.assignment) == 2
    assert len(r2.assignment) == 2
    assert r2.generation > r1.generation


async def test_leave_triggers_rebalance(broker: Broker) -> None:
    await broker.join_group("g", member_id="m1", subscription=("beats",))
    await broker.join_group("g", member_id="m2", subscription=("beats",))
    await broker.leave_group("g", "m2")
    desc = await broker.describe_group("g")
    assert [m.member_id for m in desc.members] == ["m1"]
    # m1 now owns everything again.
    rejoined = await broker.join_group("g", member_id="m1", subscription=("beats",))
    assert len(rejoined.assignment) == 4


async def test_heartbeat_stale_generation_requires_rejoin(broker: Broker) -> None:
    r1 = await broker.join_group("g", member_id="m1", subscription=("beats",))
    await broker.join_group("g", member_id="m2", subscription=("beats",))  # bumps generation
    # m1's heartbeat at its old generation reports it must rejoin.
    alive = await broker.heartbeat("g", "m1", r1.generation)
    assert alive is False


async def test_heartbeat_unknown_member(broker: Broker) -> None:
    await broker.join_group("g", member_id="m1", subscription=("beats",))
    with pytest.raises(UnknownMemberError):
        await broker.heartbeat("g", "ghost", 1)


async def test_stale_generation_commit_rejected(broker: Broker) -> None:
    r1 = await broker.join_group("g", member_id="m1", subscription=("beats",))
    await broker.join_group("g", member_id="m2", subscription=("beats",))  # current gen > r1.gen
    with pytest.raises(CommitConflictError):
        await broker.commit_offsets(
            "g", {TopicPartition("beats", 0): 5}, generation=r1.generation
        )


async def test_two_consumers_in_group_partition_the_work(broker: Broker) -> None:
    c1 = Consumer(broker, config=ConsumerConfig(group_id="renderers"))
    c2 = Consumer(broker, config=ConsumerConfig(group_id="renderers"))
    await c1.subscribe(("beats",))
    await c2.subscribe(("beats",))
    # After both join, re-subscribe c1 so it picks up the rebalanced assignment.
    await c1.subscribe(("beats",))
    owned = set(c1.assignment) | set(c2.assignment)
    assert owned == {TopicPartition("beats", p) for p in range(4)}
    assert set(c1.assignment).isdisjoint(set(c2.assignment))


# --------------------------------------------------------------------------- #
# Coordinator unit tests (deterministic clock for session timeout)
# --------------------------------------------------------------------------- #


def test_coordinator_session_timeout_evicts_dead_member() -> None:
    now = [0.0]
    coord = GroupCoordinator(
        partition_counts=lambda _t: 2,
        clock=lambda: now[0],
        session_timeout_s=10.0,
    )
    coord.join("g", member_id="m1", subscription=("t",))
    coord.join("g", member_id="m2", subscription=("t",))
    assert len(coord.describe("g").members) == 2

    # m2 stops heartbeating; advance the clock past the timeout, m1 heartbeats.
    now[0] = 100.0
    coord.heartbeat("g", "m1", coord.describe("g").generation)
    assert [m.member_id for m in coord.describe("g").members] == ["m1"]
