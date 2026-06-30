"""Integrity checksums + manifest verification + chain resolution."""

from __future__ import annotations

import pytest

from app.dr.checksums import Checksum, canonical_bytes, combine, digest
from app.dr.errors import ChainError, IntegrityError, ManifestError
from app.dr.manifest import resolve_chain, verify_manifest
from app.dr.models import BackupManifest, BackupTier, ChecksumModel, SegmentKind
from app.dr.tiers import make_full, make_incremental
from tests.dr.fixtures import World

# -- checksum primitives ---------------------------------------------------- #


def test_canonical_bytes_is_order_independent() -> None:
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


def test_digest_changes_when_a_field_changes() -> None:
    base = {"x": 1, "y": [1, 2, 3]}
    flipped = {"x": 1, "y": [1, 2, 4]}
    assert digest(base) != digest(flipped)


def test_checksum_matches_roundtrip() -> None:
    c = Checksum.of({"hello": "world"})
    assert c.matches({"hello": "world"})
    assert not c.matches({"hello": "mars"})


def test_combine_is_order_independent() -> None:
    a = Checksum.of("a")
    b = Checksum.of("b")
    assert combine(a, b).value == combine(b, a).value


# -- manifest verification -------------------------------------------------- #


async def _full(world: World, sid: str = "bk1") -> BackupManifest:
    return await make_full(
        snapshot_id=sid,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )


async def test_verify_manifest_passes_for_a_fresh_snapshot() -> None:
    world = World()
    world.add_shot("s1", page=1)
    await world.rebuild_read_model_from_events()
    manifest = await _full(world)
    verify_manifest(manifest)  # no raise


async def test_integrity_checksum_catches_segment_corruption() -> None:
    """Flipping one byte of a segment payload is caught on verify."""
    world = World()
    world.add_shot("s1", page=1)
    manifest = await _full(world)

    events_seg = manifest.segment(SegmentKind.EVENTS)
    assert events_seg is not None
    # Tamper with the payload but leave the (now-stale) checksum in place.
    events_seg.payload[0]["payload"]["page"] = 999

    with pytest.raises(IntegrityError) as exc:
        verify_manifest(manifest)
    assert exc.value.segment == str(SegmentKind.EVENTS)


async def test_integrity_catches_content_hash_tampering() -> None:
    world = World()
    world.add_shot("s1", page=1)
    manifest = await _full(world)
    # Forge the roll-up hash while leaving segments intact.
    manifest.descriptor.content_hash = ChecksumModel(value="0" * 64)
    with pytest.raises(IntegrityError) as exc:
        verify_manifest(manifest)
    assert exc.value.segment == "content_hash"


async def test_manifest_missing_segment_is_rejected() -> None:
    world = World()
    world.add_shot("s1", page=1)
    manifest = await _full(world)
    manifest.segments = [s for s in manifest.segments if s.kind is not SegmentKind.CANON]
    with pytest.raises(ManifestError):
        verify_manifest(manifest)


# -- chain resolution ------------------------------------------------------- #


async def test_resolve_chain_orders_full_then_incrementals() -> None:
    world = World()
    world.add_shot("s1", page=1)
    full = await _full(world, "full")
    await world.repo.save(full)

    world.add_shot("s2", page=2)
    inc1 = await make_incremental(
        snapshot_id="inc1",
        parent=full,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    await world.repo.save(inc1)

    world.add_shot("s3", page=3)
    inc2 = await make_incremental(
        snapshot_id="inc2",
        parent=inc1,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    await world.repo.save(inc2)

    chain = await resolve_chain(world.repo, "inc2")
    assert [m.descriptor.snapshot_id for m in chain] == ["full", "inc1", "inc2"]
    assert chain[0].descriptor.tier is BackupTier.FULL


async def test_resolve_chain_detects_missing_parent() -> None:
    world = World()
    world.add_shot("s1", page=1)
    full = await _full(world, "full")
    await world.repo.save(full)
    world.add_shot("s2", page=2)
    inc1 = await make_incremental(
        snapshot_id="inc1",
        parent=full,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    await world.repo.save(inc1)
    # Delete the founding full → chain is broken.
    await world.repo.delete("full")
    with pytest.raises(ChainError):
        await resolve_chain(world.repo, "inc1")


async def test_resolve_chain_detects_gap() -> None:
    """An incremental whose base doesn't meet its parent's pin is rejected.

    A well-formed incremental has ``base_position == parent.pinned_position``;
    here we forge a gap (base above the parent's pin) and prove the chain walk
    rejects the non-contiguous lineage rather than restoring a partial log.
    """
    world = World()
    world.add_shot("s1", page=1)
    world.add_shot("s2", page=2)
    full = await _full(world, "full")  # pins at 2
    await world.repo.save(full)

    world.add_shot("s3", page=3)
    world.add_shot("s4", page=4)
    inc1 = await make_incremental(
        snapshot_id="inc1",
        parent=full,
        event_source=world.events,
        canon=world.canon,
        read_models=world.read_models,
        assets=world.assets,
    )
    # Forge a gap: pretend the incremental starts above its parent's pin.
    inc1.descriptor.base_position = full.descriptor.pinned_position + 1
    await world.repo.save(inc1)

    with pytest.raises(ChainError):
        await resolve_chain(world.repo, "inc1", verify=False)
