"""Consistent-snapshot engine: the pin fixes the instant; segments cohere."""

from __future__ import annotations

import pytest

from app.dr.errors import ChainError, SnapshotError
from app.dr.models import BackupManifest, BackupTier, SegmentKind
from app.dr.snapshot import capture_snapshot
from tests.dr.fixtures import World


async def _full(world: World, snapshot_id: str = "bk1") -> BackupManifest:
    return await capture_snapshot(
        snapshot_id=snapshot_id,
        tier=BackupTier.FULL,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
        checkpoints={"shot_board": await world.events.head_position()},
    )


async def test_snapshot_pins_event_position_at_head() -> None:
    world = World()
    world.add_shot("s1", page=1)
    world.add_shot("s2", page=2)
    await world.rebuild_read_model_from_events()

    manifest = await _full(world)

    # The pin is the head position at capture; the event slice carries exactly
    # those events and nothing past the pin.
    assert manifest.descriptor.pinned_position == 2
    assert manifest.descriptor.base_position == 0
    events_seg = manifest.segment(SegmentKind.EVENTS)
    assert events_seg is not None
    positions = [int(e["global_position"]) for e in events_seg.payload]
    assert positions == [1, 2]


async def test_snapshot_excludes_events_appended_after_the_pin() -> None:
    """An append landing after the pin must not leak into the captured slice."""
    world = World()
    world.add_shot("s1", page=1)
    manifest = await _full(world)  # pins at position 1
    # A concurrent append after the pin:
    world.add_shot("s2", page=2)

    events_seg = manifest.segment(SegmentKind.EVENTS)
    assert events_seg is not None
    positions = [int(e["global_position"]) for e in events_seg.payload]
    assert positions == [1]  # s2 (position 2) is excluded — the pin held.


async def test_snapshot_asset_manifest_matches_captured_canon() -> None:
    world = World()
    world.add_entity("char_elsa", "Elsa", ref_key="refs/book/elsa/front.png")
    world.add_shot("s1", page=3)
    await world.rebuild_read_model_from_events()

    manifest = await _full(world)
    asset_seg = manifest.segment(SegmentKind.ASSET_MANIFEST)
    assert asset_seg is not None
    keys = {ref["key"] for ref in asset_seg.payload}
    # Every asset the captured canon/episodic state references is in the manifest.
    assert keys == {
        "refs/book/elsa/front.png",
        "clips/book/s1.mp4",
        "audio/book/s1.wav",
    }
    # And every entry carries a non-empty content digest (the asset was present).
    assert all(ref["checksum"]["value"] for ref in asset_seg.payload)


async def test_snapshot_records_missing_asset_with_empty_digest() -> None:
    """A referenced-but-absent asset is recorded (digest empty), not dropped."""
    world = World()
    world.add_shot("s1", page=1, with_assets=False)  # event/episodic but no bytes
    manifest = await _full(world)
    asset_seg = manifest.segment(SegmentKind.ASSET_MANIFEST)
    assert asset_seg is not None
    by_key = {r["key"]: r for r in asset_seg.payload}
    assert by_key["clips/book/s1.mp4"]["checksum"]["value"] == ""


async def test_full_with_parent_is_rejected() -> None:
    world = World()
    with pytest.raises(ChainError):
        await capture_snapshot(
            snapshot_id="bk1",
            tier=BackupTier.FULL,
            event_source=world.events,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
            parent_id="oops",
            base_position=5,
        )


async def test_incremental_without_parent_is_rejected() -> None:
    world = World()
    world.add_shot("s1", page=1)
    with pytest.raises(ChainError):
        await capture_snapshot(
            snapshot_id="bk2",
            tier=BackupTier.INCREMENTAL,
            event_source=world.events,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
        )


async def test_incremental_base_ahead_of_head_is_rejected() -> None:
    world = World()
    world.add_shot("s1", page=1)  # head == 1
    with pytest.raises(SnapshotError):
        await capture_snapshot(
            snapshot_id="bk2",
            tier=BackupTier.INCREMENTAL,
            event_source=world.events,
            canon=world.canon,
            read_models=world.read_models,
            assets=world.assets,
            parent_id="bk1",
            base_position=5,  # parent claims a pin past the current head
        )


async def test_content_hash_is_deterministic_and_covers_segments() -> None:
    """Two captures of the same world produce the same content hash."""
    world = World()
    world.add_shot("s1", page=1)
    await world.rebuild_read_model_from_events()
    a = await _full(world, "bkA")
    b = await _full(world, "bkB")
    assert a.descriptor.content_hash.value == b.descriptor.content_hash.value
