"""Tests for geo-routing (routing.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.clock import NodeId
from app.distributed.replication.routing import (
    GeoRouter,
    NoRouteError,
    PlacementPolicy,
    RegionTopology,
)
from app.distributed.replication.store import KeyAffinity

US = NodeId("us", "1")
EU = NodeId("eu", "1")
AP = NodeId("ap", "1")

TOPO = RegionTopology.from_nodes(
    [US, EU, AP],
    latency_ms={
        ("us", "eu"): 80,
        ("us", "ap"): 150,
        ("eu", "us"): 80,
        ("eu", "ap"): 120,
        ("ap", "us"): 150,
        ("ap", "eu"): 120,
    },
)


def test_topology_groups_by_region() -> None:
    assert TOPO.regions == {"us", "eu", "ap"}
    assert TOPO.nodes("us") == {US}
    assert TOPO.all_nodes() == {US, EU, AP}


def test_topology_latency_lookup_and_defaults() -> None:
    assert TOPO.latency("us", "us") == 0
    assert TOPO.latency("us", "eu") == 80
    bare = RegionTopology.from_nodes([US, EU], default_latency_ms=99)
    assert bare.latency("us", "eu") == 99


def test_placement_full_replication_without_affinity() -> None:
    pol = PlacementPolicy(TOPO)
    assert pol.replica_regions(None) == {"us", "eu", "ap"}


def test_placement_narrows_to_affinity_replicas() -> None:
    pol = PlacementPolicy(TOPO)
    aff = KeyAffinity(home_region="us", replicas=frozenset({"eu"}))
    # home is always a replica even if not listed.
    assert pol.replica_regions(aff) == {"us", "eu"}
    assert pol.replica_nodes(aff) == {US, EU}


def test_route_prefers_local_region() -> None:
    router = GeoRouter(TOPO)
    decision = router.route("k", client_region="eu")
    assert decision.node == EU
    assert decision.reason == "local-region"
    assert decision.latency_ms == 0


def test_route_honours_home_region_when_not_local() -> None:
    router = GeoRouter(TOPO)
    aff = KeyAffinity(home_region="ap", replicas=frozenset({"ap", "us"}))
    # client in us; us replicates but home is ap -> local us wins (rule 1).
    local = router.route("k", client_region="us", affinity=aff)
    assert local.node == US and local.reason == "local-region"
    # client in eu, which does NOT replicate this key -> falls to home ap.
    routed = router.route("k", client_region="eu", affinity=aff)
    assert routed.node == AP
    assert routed.reason == "home-region"
    assert routed.latency_ms == 120


def test_route_picks_nearest_replica_when_no_local_or_home() -> None:
    router = GeoRouter(TOPO)
    # affinity replicates only in us and ap; client in eu, home is us.
    # rule 2 (home) fires first -> us. Make home a dead-letter by routing from a
    # region where home is excluded: use replicas without home for rule-3 path.
    aff = KeyAffinity(home_region="us", replicas=frozenset({"us", "ap"}))
    routed = router.route("k", client_region="eu", affinity=aff)
    # home us reachable -> rule 2.
    assert routed.node == US


def test_route_nearest_replica_rule_three() -> None:
    # A key whose home region has no live node forces the nearest-replica path.
    router = GeoRouter(TOPO, liveness=lambda n: n != US)
    aff = KeyAffinity(home_region="us", replicas=frozenset({"us", "eu", "ap"}))
    routed = router.route("k", client_region="us", affinity=aff)
    # us dead; from us, eu (80) is nearer than ap (150).
    assert routed.node == EU
    assert routed.reason == "nearest-replica"
    assert routed.latency_ms == 80


def test_route_raises_when_no_live_replica() -> None:
    router = GeoRouter(TOPO, liveness=lambda _n: False)
    with pytest.raises(NoRouteError):
        router.route("k", client_region="us")


def test_live_replica_set_excludes_dead_nodes() -> None:
    router = GeoRouter(TOPO, liveness=lambda n: n != AP)
    assert router.replica_set() == {US, EU, AP}
    assert router.live_replica_set() == {US, EU}
