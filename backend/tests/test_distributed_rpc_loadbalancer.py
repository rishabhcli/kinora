"""Tests for the load-balancing policies."""

from __future__ import annotations

from app.distributed.rpc.loadbalancer import (
    InFlightTracker,
    LoadBalancePolicy,
    LoadBalancer,
)
from app.distributed.rpc.registry import InstanceHealth, ServiceInstance


def _inst(iid: str, *, healthy: bool = True, weight: int = 1) -> ServiceInstance:
    return ServiceInstance(
        service="svc",
        instance_id=iid,
        health=healthy,
        health_status=InstanceHealth.HEALTHY if healthy else InstanceHealth.UNHEALTHY,
        weight=weight,
    )


def test_filters_unhealthy() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.ROUND_ROBIN)
    insts = [_inst("a", healthy=False), _inst("b")]
    for _ in range(3):
        assert lb.pick(insts).instance_id == "b"  # type: ignore[union-attr]


def test_none_when_all_unhealthy() -> None:
    lb = LoadBalancer()
    assert lb.pick([_inst("a", healthy=False)]) is None
    assert lb.pick([]) is None


def test_round_robin_cycles() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.ROUND_ROBIN)
    insts = [_inst("a"), _inst("b"), _inst("c")]
    picks = [lb.pick(insts).instance_id for _ in range(6)]  # type: ignore[union-attr]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_least_connections_prefers_idle() -> None:
    tracker = InFlightTracker()
    lb = LoadBalancer(policy=LoadBalancePolicy.LEAST_CONNECTIONS, tracker=tracker)
    insts = [_inst("a"), _inst("b"), _inst("c")]
    tracker.inc("a")
    tracker.inc("a")
    tracker.inc("b")
    assert lb.pick(insts).instance_id == "c"  # type: ignore[union-attr]


def test_p2c_picks_lighter_of_two() -> None:
    tracker = InFlightTracker()
    lb = LoadBalancer(policy=LoadBalancePolicy.P2C, tracker=tracker, seed=3)
    insts = [_inst("a"), _inst("b")]
    tracker.inc("a")  # a busier than b
    # With 2 instances P2C samples both; should pick the lighter ("b").
    assert lb.pick(insts).instance_id == "b"  # type: ignore[union-attr]


def test_consistent_hash_is_sticky() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.CONSISTENT_HASH)
    insts = [_inst("a"), _inst("b"), _inst("c")]
    first = lb.pick(insts, hash_key="session-42").instance_id  # type: ignore[union-attr]
    for _ in range(10):
        assert lb.pick(insts, hash_key="session-42").instance_id == first  # type: ignore[union-attr]


def test_consistent_hash_minimal_remap_on_removal() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.CONSISTENT_HASH)
    full = [_inst("a"), _inst("b"), _inst("c"), _inst("d")]
    keys = [f"k{i}" for i in range(200)]
    before = {k: lb.pick(full, hash_key=k).instance_id for k in keys}  # type: ignore[union-attr]
    # Remove instance "c"; only keys that hashed to "c" should move.
    reduced = [i for i in full if i.instance_id != "c"]
    after = {k: lb.pick(reduced, hash_key=k).instance_id for k in keys}  # type: ignore[union-attr]
    moved = [k for k in keys if before[k] != after[k]]
    # Every moved key previously mapped to the removed instance (rendezvous prop).
    assert all(before[k] == "c" for k in moved)
    # And nothing should now route to "c".
    assert all(v != "c" for v in after.values())


def test_consistent_hash_without_key_falls_back_to_rr() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.CONSISTENT_HASH)
    insts = [_inst("a"), _inst("b")]
    picks = {lb.pick(insts).instance_id for _ in range(4)}  # type: ignore[union-attr]
    assert picks == {"a", "b"}


def test_single_instance_shortcuts() -> None:
    lb = LoadBalancer(policy=LoadBalancePolicy.P2C)
    assert lb.pick([_inst("solo")]).instance_id == "solo"  # type: ignore[union-attr]


def test_inflight_tracker_floors_at_zero() -> None:
    t = InFlightTracker()
    t.dec("a")
    assert t.get("a") == 0
    t.inc("a")
    t.inc("a")
    t.dec("a")
    assert t.get("a") == 1
