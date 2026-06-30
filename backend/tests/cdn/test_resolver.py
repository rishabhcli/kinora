"""Resolver: nearest healthy replica + lag/availability skipping + origin failover."""

from __future__ import annotations

import pytest

from app.cdn.errors import NoHealthyReplicaError, OriginMissingObjectError
from app.cdn.regions import GeoPoint, ReaderHint, RegionHealth
from app.cdn.replication import ReplicationManager
from app.cdn.resolver import AssetResolver
from app.cdn.testing import FakeClock, FakeRegionStore, demo_topology

KEY = "clips/book1/shot_00007.mp4"
DATA = b"clip bytes" * 32

# A reader near Frankfurt — geo-nearest to the EU replica.
EU_READER = ReaderHint(geo=GeoPoint(lat=50.1, lon=8.7))


async def _resolver(
    *, public: bool = False
) -> tuple[AssetResolver, ReplicationManager, dict[str, FakeRegionStore], FakeClock]:
    topo = demo_topology()
    base = "https://cdn.example.com" if public else None
    stores = {rid: FakeRegionStore(rid, public_base_url=base) for rid in topo.region_ids}
    clk = FakeClock()
    mgr = ReplicationManager(topology=topo, stores=stores, clock=clk)
    resolver = AssetResolver(manager=mgr, clock=clk)
    return resolver, mgr, stores, clk


async def test_resolves_to_nearest_replica_when_present() -> None:
    resolver, mgr, stores, _ = await _resolver()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    res = await resolver.resolve(KEY, EU_READER)

    assert not res.served_from_origin
    assert res.region_id == "eu"
    assert "eu.fake-s3.local" in res.signed_url.url


async def test_failover_to_origin_when_no_replica_has_object() -> None:
    resolver, mgr, stores, _ = await _resolver()
    stores["na"].seed(KEY, DATA)  # origin only; never replicated

    res = await resolver.resolve(KEY, EU_READER)

    assert res.served_from_origin
    assert res.region_id == "na"
    # Both replicas were considered and skipped as "missing".
    assert {rid for rid, _ in res.skipped} == {"eu", "ap"}
    assert all(reason == "missing" for _, reason in res.skipped)


async def test_skips_stale_replica_and_uses_next_nearest() -> None:
    resolver, mgr, stores, _ = await _resolver()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    # EU is geo-nearest but reported far behind origin -> skipped for AP.
    health = {"eu": RegionHealth(region_id="eu", replication_lag_s=999.0)}
    res = await resolver.resolve(KEY, EU_READER, health=health)

    assert not res.served_from_origin
    assert res.region_id == "ap"
    assert ("eu", "stale(lag=999.0s)") in res.skipped


async def test_skips_unavailable_replica() -> None:
    resolver, mgr, stores, _ = await _resolver()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    health = {"eu": RegionHealth(region_id="eu", available=False)}
    res = await resolver.resolve(KEY, EU_READER, health=health)

    assert res.region_id == "ap"
    assert ("eu", "unavailable") in res.skipped


async def test_no_healthy_region_raises_when_origin_down_and_replicas_skipped() -> None:
    resolver, mgr, stores, _ = await _resolver()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    health = {
        "na": RegionHealth(region_id="na", available=False),
        "eu": RegionHealth(region_id="eu", available=False),
        "ap": RegionHealth(region_id="ap", available=False),
    }
    with pytest.raises(NoHealthyReplicaError):
        await resolver.resolve(KEY, EU_READER, health=health)


async def test_origin_missing_object_raises_on_failover() -> None:
    resolver, _, _, _ = await _resolver()
    # Nothing seeded anywhere; failover finds origin empty.
    with pytest.raises(OriginMissingObjectError):
        await resolver.resolve(KEY, EU_READER)


async def test_signed_url_expiry_recorded() -> None:
    resolver, mgr, stores, clk = await _resolver()
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    res = await resolver.resolve(KEY, EU_READER, ttl=120)
    assert res.signed_url.expires_at == pytest.approx(clk.now() + 120)
    assert res.signed_url.range_supported is True


async def test_public_base_url_resolution_has_no_expiry() -> None:
    resolver, mgr, stores, _ = await _resolver(public=True)
    stores["na"].seed(KEY, DATA)
    await mgr.replicate(KEY)

    res = await resolver.resolve(KEY, EU_READER)
    assert res.signed_url.expires_at is None
    assert res.signed_url.url == f"https://cdn.example.com/{KEY}"
