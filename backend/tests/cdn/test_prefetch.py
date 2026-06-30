"""Prefetch: warm upcoming shots into the reader's nearest region ahead of play."""

from __future__ import annotations

from app.cdn.prefetch import PrefetchController, PrefetchOutcome
from app.cdn.regions import GeoPoint, ReaderHint, RegionHealth
from app.cdn.replication import ReplicationManager
from app.cdn.testing import FakeCdnProvider, FakeClock, FakeRegionStore, demo_topology
from app.storage.object_store import keys as object_keys

BOOK = "book1"
SHOTS = ["shot_00010", "shot_00011", "shot_00012", "shot_00013", "shot_00014"]
EU_READER = ReaderHint(geo=GeoPoint(lat=50.1, lon=8.7))


def _setup(
    *, max_warm: int = 4, with_edges: bool = True
) -> tuple[PrefetchController, dict[str, FakeRegionStore], dict[str, FakeCdnProvider]]:
    topo = demo_topology()
    stores = {rid: FakeRegionStore(rid) for rid in topo.region_ids}
    # Origin holds every upcoming clip (the render pipeline persisted them).
    for shot in SHOTS:
        stores["na"].seed(object_keys.clip(BOOK, shot), f"clip-{shot}".encode())
    clk = FakeClock()
    mgr = ReplicationManager(topology=topo, stores=stores, clock=clk)
    providers = (
        {rid: FakeCdnProvider(rid) for rid in topo.region_ids} if with_edges else {}
    )
    ctrl = PrefetchController(manager=mgr, providers=providers, max_warm=max_warm)
    return ctrl, stores, providers


async def test_warm_upcoming_replicates_and_warms_nearest_region() -> None:
    ctrl, stores, providers = _setup()

    plan = await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER)

    assert plan.region_id == "eu"  # geo-nearest replica
    assert plan.warmed == 4  # capped at max_warm
    # The first 4 clips are now replicated to EU and warm in EU's edge.
    for shot in SHOTS[:4]:
        key = object_keys.clip(BOOK, shot)
        assert await stores["eu"].exists(key)
        assert key in providers["eu"].warmed
    # The 5th shot was beyond the cap and not warmed.
    assert object_keys.clip(BOOK, SHOTS[4]) not in providers["eu"].warmed


async def test_prefetch_is_idempotent_already_warm() -> None:
    ctrl, _, providers = _setup()
    await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER)
    warmed_count_after_first = len(providers["eu"].warmed)

    plan2 = await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER)

    assert all(r.outcome is PrefetchOutcome.ALREADY_WARM for r in plan2.results)
    # No new warm calls issued on the idempotent re-run.
    assert len(providers["eu"].warmed) == warmed_count_after_first


async def test_prefetch_picks_ap_when_eu_unavailable() -> None:
    ctrl, stores, providers = _setup()
    health = {"eu": RegionHealth(region_id="eu", available=False)}

    plan = await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER, health=health)

    assert plan.region_id == "ap"
    assert await stores["ap"].exists(object_keys.clip(BOOK, SHOTS[0]))


async def test_prefetch_without_edge_provider_replicates_only() -> None:
    ctrl, stores, _ = _setup(with_edges=False)

    plan = await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER)

    assert plan.region_id == "eu"
    assert all(r.outcome is PrefetchOutcome.REPLICATED_NO_EDGE for r in plan.results)
    # Replication still happened even with no edge to warm.
    assert await stores["eu"].exists(object_keys.clip(BOOK, SHOTS[0]))


async def test_prefetch_missing_origin_clip_is_best_effort_failure() -> None:
    ctrl, _, _ = _setup()
    # A shot that origin doesn't have yet (still rendering) must not raise.
    plan = await ctrl.warm_keys([object_keys.clip(BOOK, "shot_99999")], EU_READER)
    assert plan.results[0].outcome is PrefetchOutcome.FAILED
    assert plan.results[0].detail is not None


async def test_warm_falls_back_to_origin_when_all_replicas_down() -> None:
    ctrl, stores, providers = _setup()
    health = {
        "eu": RegionHealth(region_id="eu", available=False),
        "ap": RegionHealth(region_id="ap", available=False),
    }
    plan = await ctrl.warm_upcoming(BOOK, SHOTS, EU_READER, health=health)
    # No replica available -> warm origin's own edge (no replication needed).
    assert plan.region_id == "na"
    assert object_keys.clip(BOOK, SHOTS[0]) in providers["na"].warmed
