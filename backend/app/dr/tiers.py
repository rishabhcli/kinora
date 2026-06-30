"""Full + incremental backup tiers over the snapshot engine.

Thin, declarative wrappers that turn the low-level
:func:`app.dr.snapshot.capture_snapshot` into the two operator-facing tiers and
keep their wiring honest:

* :func:`make_full` captures the whole world (``base_position == 0``, no parent).
* :func:`make_incremental` captures only the events appended since a *parent*
  snapshot's pin, threading the parent's ``pinned_position`` through as the new
  ``base_position`` so the resulting chain is contiguous and gap-free — exactly
  what :func:`app.dr.manifest.resolve_chain` later validates.

The incremental still re-captures the full canon / read-model / asset *state* as
of the new pin (an incremental of *events*, a full snapshot of *derived state*).
That keeps restore simple and correct: replaying a chain's concatenated event
slices and loading the head's derived state lands at exactly the materialised
state a full backup at the head's pin would have produced — the property the
"full+incremental chain restore equals source" test asserts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from app.dr.errors import ChainError
from app.dr.interfaces import AssetSource, CanonSource, EventSource, ReadModelTarget
from app.dr.models import BackupManifest, BackupTier, SnapshotDescriptor


async def make_full(
    *,
    snapshot_id: str,
    event_source: EventSource,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    checkpoints: Mapping[str, int] | None = None,
    now: datetime | None = None,
    labels: Mapping[str, str] | None = None,
) -> BackupManifest:
    """Capture a self-contained full backup (the chain root)."""
    from app.dr.snapshot import capture_snapshot

    return await capture_snapshot(
        snapshot_id=snapshot_id,
        tier=BackupTier.FULL,
        event_source=event_source,
        canon=canon,
        read_models=read_models,
        assets=assets,
        checkpoints=checkpoints,
        base_position=0,
        parent_id=None,
        now=now,
        labels=labels,
    )


async def make_incremental(
    *,
    snapshot_id: str,
    parent: SnapshotDescriptor | BackupManifest,
    event_source: EventSource,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    checkpoints: Mapping[str, int] | None = None,
    now: datetime | None = None,
    labels: Mapping[str, str] | None = None,
) -> BackupManifest:
    """Capture an incremental backup chained onto ``parent``.

    The new snapshot's event slice is ``(parent.pinned_position, new_pin]`` so it
    carries only the events appended since the parent. The derived state (canon /
    read models / asset manifest) is re-captured in full as of ``new_pin``.

    Raises:
        ChainError: ``parent`` resolves to a tier other than a backup we can
            chain onto (i.e. it has no usable ``pinned_position``).
    """
    from app.dr.snapshot import capture_snapshot

    desc = parent.descriptor if isinstance(parent, BackupManifest) else parent
    if desc.pinned_position < 0:
        raise ChainError("parent snapshot has an invalid pinned_position")

    return await capture_snapshot(
        snapshot_id=snapshot_id,
        tier=BackupTier.INCREMENTAL,
        event_source=event_source,
        canon=canon,
        read_models=read_models,
        assets=assets,
        checkpoints=checkpoints,
        base_position=desc.pinned_position,
        parent_id=desc.snapshot_id,
        now=now,
        labels=labels,
    )


__all__ = ["make_full", "make_incremental"]
