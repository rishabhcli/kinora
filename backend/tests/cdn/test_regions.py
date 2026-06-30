"""Region topology + geo/latency nearest-region scoring (deterministic, offline)."""

from __future__ import annotations

import pytest

from app.cdn.errors import NoOriginError, UnknownRegionError
from app.cdn.regions import (
    UNKNOWN_HINT_RTT_MS,
    GeoPoint,
    ReaderHint,
    Region,
    RegionHealth,
    RegionTopology,
    haversine_km,
)
from app.cdn.testing import demo_topology


def test_topology_origin_and_replicas() -> None:
    topo = demo_topology()
    assert topo.origin.region_id == "na"
    assert set(topo.replica_ids()) == {"eu", "ap"}
    assert topo.region_ids == ("na", "eu", "ap")


def test_topology_rejects_no_origin() -> None:
    with pytest.raises(NoOriginError):
        RegionTopology([Region(region_id="na"), Region(region_id="eu")])


def test_topology_rejects_two_origins() -> None:
    with pytest.raises(ValueError, match="multiple origin"):
        RegionTopology(
            [Region(region_id="na", origin=True), Region(region_id="eu", origin=True)]
        )


def test_topology_rejects_duplicate_id() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        RegionTopology([Region(region_id="na", origin=True), Region(region_id="na")])


def test_get_unknown_region_raises() -> None:
    topo = demo_topology()
    with pytest.raises(UnknownRegionError):
        topo.get("antarctica")


def test_haversine_is_symmetric_and_zero_on_same_point() -> None:
    a = GeoPoint(lat=10.0, lon=20.0)
    b = GeoPoint(lat=-30.0, lon=140.0)
    assert haversine_km(a, a) == pytest.approx(0.0, abs=1e-6)
    assert haversine_km(a, b) == pytest.approx(haversine_km(b, a))


def test_explicit_region_hint_wins() -> None:
    topo = demo_topology()
    ranked = topo.rank(ReaderHint(region_id="ap"))
    assert ranked[0][0] == "ap"
    assert ranked[0][1] == 0.0


def test_geo_hint_picks_nearest_replica() -> None:
    topo = demo_topology()
    # A reader near Frankfurt should rank EU ahead of NA/AP.
    ranked = topo.rank(ReaderHint(geo=GeoPoint(lat=50.1, lon=8.7)))
    assert ranked[0][0] == "eu"
    # And the AP replica (Singapore) should be the farthest of the three.
    assert ranked[-1][0] == "ap"


def test_country_maps_to_continent_affinity() -> None:
    topo = demo_topology()
    # India -> "ap" continent; AP region should win on continent affinity even
    # without coordinates.
    ranked = topo.rank(ReaderHint(country="IN"))
    assert ranked[0][0] == "ap"


def test_measured_rtt_overrides_geo() -> None:
    topo = demo_topology()
    # Geo says EU is nearest, but a measured RTT makes AP cheapest.
    health = {
        "eu": RegionHealth(region_id="eu", rtt_ms=200.0),
        "ap": RegionHealth(region_id="ap", rtt_ms=5.0),
    }
    ranked = topo.rank(
        ReaderHint(geo=GeoPoint(lat=50.1, lon=8.7)), health=health
    )
    assert ranked[0][0] == "ap"


def test_unknown_hint_is_finite_and_tie_breaks_on_id() -> None:
    topo = demo_topology()
    ranked = topo.rank(ReaderHint())  # no hint at all
    # Every region gets the same unknown cost, so order is by id.
    assert {rid for rid, _ in ranked} == {"na", "eu", "ap"}
    assert all(cost == UNKNOWN_HINT_RTT_MS for _, cost in ranked)
    assert [rid for rid, _ in ranked] == sorted(rid for rid, _ in ranked)
