"""The consistent snapshot engine — capture a coherent point in time.

A backup is only useful if it is *internally consistent*: the canon, the read
models, and the asset manifest must all reflect the world at one well-defined
moment, and no event past that moment may leak into the captured event slice. We
achieve that by **pinning the event-store head position first** and treating that
pinned position as the single source of truth for "now":

    pin  = event_source.head_position()          # 1. fix the instant
    canon, checkpoints, read_models = capture()  # 2. snapshot derived state
    events(base, pin] = event_source.read_range  # 3. only events up to the pin
    asset_manifest = build_from(canon + episodic) # 4. assets the state references

Because the event store is append-only and the pin is captured *before* the rest,
any concurrent append lands at ``pin + 1`` or later and is excluded from step 3
— so the captured event slice can never be "ahead of" the captured read models.
The asset manifest is derived from the *captured* canon/episodic state, so it
matches that state by construction; restore later checks the live asset source
against this manifest to catch assets lost since capture.

A **full** snapshot captures from ``base_position == 0`` (the whole log); an
**incremental** captures from its parent's pin (only the new events), but still
re-captures the canon/read-model/asset *state* as of the new pin so a chain
restore lands at exactly the same materialised state as a full at that pin.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from app.dr.checksums import Checksum, combine
from app.dr.errors import ChainError, SnapshotError
from app.dr.interfaces import AssetSource, CanonSource, EventSource, ReadModelTarget
from app.dr.models import (
    AssetRef,
    BackupManifest,
    BackupTier,
    ChecksumModel,
    Segment,
    SegmentKind,
    SnapshotDescriptor,
)

logger = structlog.get_logger(__name__)


def _checksum_model(value: Any) -> ChecksumModel:
    """Compute a segment checksum and project it to its wire model."""
    c: Checksum = Checksum.of(value)
    return ChecksumModel(algorithm=c.algorithm, value=c.value)


async def _build_asset_manifest(
    canon: CanonSource,
    assets: AssetSource,
) -> list[AssetRef]:
    """Derive the asset manifest from the canon/episodic state being captured.

    Each referenced key is resolved against the asset source for its *content*
    digest + size so restore can detect not just a missing asset but a mutated
    one. A key referenced by the canon but already absent from the source at
    capture is still recorded (digest ``None``→empty) so the inconsistency is
    visible in the backup rather than silently dropped.
    """
    refs: list[AssetRef] = []
    for key in await canon.asset_keys():
        dig = await assets.content_digest(key)
        size = await assets.size(key)
        refs.append(
            AssetRef(
                key=key,
                checksum=ChecksumModel(value=dig or ""),
                size=size,
            )
        )
    return refs


async def capture_snapshot(
    *,
    snapshot_id: str,
    tier: BackupTier,
    event_source: EventSource,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    checkpoints: Mapping[str, int] | None = None,
    base_position: int = 0,
    parent_id: str | None = None,
    now: datetime | None = None,
    labels: Mapping[str, str] | None = None,
) -> BackupManifest:
    """Capture a consistent backup as of the event store's current head.

    Args:
        snapshot_id: the id to stamp on the manifest.
        tier: FULL or INCREMENTAL (an INCREMENTAL must carry a ``parent_id`` and
            a ``base_position`` > 0).
        event_source: the log to pin + slice.
        canon: the canon/episodic source to dump.
        read_models: the read-model store to dump.
        assets: the object-store source to resolve the asset manifest against.
        checkpoints: optional ``{projection: position}`` to record (so a restore
            can reset projections to a known-consistent position before rebuild).
        base_position: capture events ``(base_position, pin]``. 0 for a full.
        parent_id: the parent snapshot id for an incremental.
        now: capture timestamp (defaults to ``datetime.now(UTC)``).
        labels: free-form labels stamped onto the descriptor.

    Returns:
        A fully-checksummed :class:`BackupManifest`.

    Raises:
        SnapshotError: an incremental with ``base_position`` past the pin (its
            parent is *ahead* of the current head — a malformed chain request),
            or a base_position below 0.
        ChainError: an incremental without a parent id / base position, or a full
            with a parent.
    """
    if tier is BackupTier.INCREMENTAL:
        if parent_id is None or base_position <= 0:
            raise ChainError("an incremental snapshot requires a parent_id and a base_position > 0")
    else:  # FULL
        if parent_id is not None or base_position != 0:
            raise ChainError("a full snapshot must have no parent and base_position == 0")

    if base_position < 0:
        raise SnapshotError(f"base_position must be >= 0, got {base_position}")

    # 1. Pin the instant FIRST — this fixes "now" for everything below.
    pin = await event_source.head_position()
    if base_position > pin:
        raise SnapshotError(
            f"base_position {base_position} is ahead of the event-store head {pin}; "
            "the parent snapshot is newer than the current log"
        )

    # 2. Capture derived state as of the pin.
    canon_state = await canon.dump()
    read_model_rows = await read_models.dump()
    checkpoint_state = dict(checkpoints or {})

    # 3. Slice the event log up to (and not past) the pin.
    events = await event_source.read_range(base_position, pin)

    # 4. Build the asset manifest from the captured canon/episodic state.
    asset_refs = await _build_asset_manifest(canon, assets)

    segments = [
        Segment(
            kind=SegmentKind.EVENTS,
            payload=events,
            checksum=_checksum_model(events),
            item_count=len(events),
        ),
        Segment(
            kind=SegmentKind.CANON,
            payload=canon_state,
            checksum=_checksum_model(canon_state),
            item_count=len(canon_state.get("entities", {})) + len(canon_state.get("episodic", {})),
        ),
        Segment(
            kind=SegmentKind.CHECKPOINTS,
            payload=checkpoint_state,
            checksum=_checksum_model(checkpoint_state),
            item_count=len(checkpoint_state),
        ),
        Segment(
            kind=SegmentKind.READ_MODELS,
            payload=read_model_rows,
            checksum=_checksum_model(read_model_rows),
            item_count=sum(len(v) for v in read_model_rows.values()),
        ),
        Segment(
            kind=SegmentKind.ASSET_MANIFEST,
            payload=[r.model_dump() for r in asset_refs],
            checksum=_checksum_model([r.model_dump() for r in asset_refs]),
            item_count=len(asset_refs),
        ),
    ]

    content_hash = combine(
        *(Checksum(algorithm=s.checksum.algorithm, value=s.checksum.value) for s in segments)
    )

    descriptor = SnapshotDescriptor(
        snapshot_id=snapshot_id,
        tier=tier,
        pinned_position=pin,
        base_position=base_position,
        parent_id=parent_id,
        created_at=now or datetime.now(UTC),
        content_hash=ChecksumModel(algorithm=content_hash.algorithm, value=content_hash.value),
        labels=dict(labels or {}),
    )

    logger.info(
        "dr.snapshot.captured",
        snapshot_id=snapshot_id,
        tier=str(tier),
        pinned_position=pin,
        base_position=base_position,
        events=len(events),
        assets=len(asset_refs),
    )
    return BackupManifest(descriptor=descriptor, segments=segments)


__all__ = ["capture_snapshot"]
