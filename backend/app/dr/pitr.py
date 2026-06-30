"""Point-in-time recovery — restore to an event position or a timestamp ``T``.

PITR is "rewind the world to exactly the moment before the mistake". Given a
target — an event ``global_position`` or a wall-clock ``timestamp`` — we resolve
the *narrowest backup chain that covers ``T``* and the *replay bound* within it,
then hand a normal restore the head id + ``through`` bound so it replays only up
to ``T`` and stops:

* **By position.** Pick the chain whose head pin is ``>= T`` and whose founding
  full's base is ``< T`` *and* with the smallest such head pin (the freshest
  chain that still contains ``T`` without overshooting). Replay ``(0, T]`` —
  the read-model rebuild folds exactly the events up to ``T``, so the recovered
  state is the world as of position ``T``.
* **By timestamp.** Resolve ``T`` to the highest event position recorded at/before
  it via :meth:`EventSource.position_at_or_before`, then recover by that position.
  (Resolution needs the live log; PITR is a recovery operation, so the source log
  — even a degraded one — is available.)

The recovered read models are always rebuilt by *re-projection* up to the bound
(never loaded verbatim), because the captured read-model segment reflects the
head's pin, not ``T``. A loaded-verbatim read model would be ahead of the
replayed events — exactly the inconsistency PITR exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.dr.errors import PointInTimeError
from app.dr.interfaces import (
    AssetSource,
    BackupRepository,
    CanonSource,
    EventSink,
    EventSource,
    ReadModelTarget,
)
from app.dr.models import BackupManifest, BackupTier
from app.dr.restore import Projector, RestorePlan, RestoreResult, restore

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RecoveryTarget:
    """The resolved recovery: which chain head + how far to replay."""

    head_id: str
    replay_through: int
    #: How ``replay_through`` was derived ("position" or "timestamp").
    resolved_from: str


async def _candidate_heads(repo: BackupRepository) -> list[BackupManifest]:
    ids = await repo.list_ids()
    out: list[BackupManifest] = []
    for sid in ids:
        m = await repo.get(sid)
        if m is not None:
            out.append(m)
    return out


async def resolve_target_position(
    repo: BackupRepository,
    position: int,
) -> RecoveryTarget:
    """Resolve a target event ``position`` to a recoverable ``(head, bound)``.

    Chooses the chain head with the smallest ``pinned_position >= position`` whose
    founding full backup's coverage starts at or before ``position`` (i.e. a full
    exists in the fleet so the chain bottoms out). Among ties prefers the head
    whose chain is shortest (cheapest replay) — here, simply the smallest pin.

    Raises:
        PointInTimeError: ``position`` is negative, no backup covers it, or no
            full backup exists to anchor a chain.
    """
    if position < 0:
        raise PointInTimeError(f"target position must be >= 0, got {position}")

    candidates = await _candidate_heads(repo)
    if not candidates:
        raise PointInTimeError("no backups exist; nothing is recoverable")

    fulls = [m for m in candidates if m.descriptor.tier is BackupTier.FULL]
    if not fulls:
        raise PointInTimeError("no full backup exists to anchor a recovery chain")

    # Position 0 (the empty world) is recoverable from the earliest full.
    if position == 0:
        earliest_full = min(fulls, key=lambda m: m.descriptor.pinned_position)
        return RecoveryTarget(
            head_id=earliest_full.descriptor.snapshot_id,
            replay_through=0,
            resolved_from="position",
        )

    # Heads whose pin reaches at least ``position``.
    covering = [m for m in candidates if m.descriptor.pinned_position >= position]
    if not covering:
        head = max(candidates, key=lambda m: m.descriptor.pinned_position)
        raise PointInTimeError(
            f"target position {position} is past the freshest backup pin "
            f"{head.descriptor.pinned_position}; not yet captured"
        )

    # Smallest pin >= position → freshest chain that contains T without overshoot.
    head = min(covering, key=lambda m: m.descriptor.pinned_position)
    return RecoveryTarget(
        head_id=head.descriptor.snapshot_id,
        replay_through=position,
        resolved_from="position",
    )


async def resolve_target_timestamp(
    repo: BackupRepository,
    event_source: EventSource,
    timestamp: float,
) -> RecoveryTarget:
    """Resolve a wall-clock ``timestamp`` (epoch s) to a recoverable target.

    Maps ``timestamp`` to the highest event position recorded at/before it, then
    delegates to :func:`resolve_target_position`.

    Raises:
        PointInTimeError: no event was recorded at/before ``timestamp``.
    """
    position = await event_source.position_at_or_before(timestamp)
    if position == 0:
        # Either the log is empty before T, or T precedes the first event.
        # Position 0 is still a valid recovery (the empty world) iff a full exists.
        target = await resolve_target_position(repo, 0)
        return RecoveryTarget(
            head_id=target.head_id,
            replay_through=0,
            resolved_from="timestamp",
        )
    target = await resolve_target_position(repo, position)
    return RecoveryTarget(
        head_id=target.head_id,
        replay_through=target.replay_through,
        resolved_from="timestamp",
    )


async def recover_to_position(
    repo: BackupRepository,
    position: int,
    *,
    event_sink: EventSink,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    projector: Projector,
    dry_run: bool = False,
    require_assets: bool = True,
) -> tuple[RecoveryTarget, RestorePlan, RestoreResult | None]:
    """Recover state to exactly event ``position`` (PITR by position).

    A ``projector`` is **required**: PITR rebuilds read models by re-projecting
    the events replayed up to the bound, never by loading the captured (head-pin)
    rows, so the recovered read models are consistent with the truncated log.
    """
    target = await resolve_target_position(repo, position)
    plan, result = await restore(
        repo,
        target.head_id,
        event_sink=event_sink,
        canon=canon,
        read_models=read_models,
        assets=assets,
        through=target.replay_through,
        projector=projector,
        dry_run=dry_run,
        require_assets=require_assets,
    )
    logger.info(
        "dr.pitr.position",
        position=position,
        head_id=target.head_id,
        bound=target.replay_through,
        dry_run=dry_run,
    )
    return target, plan, result


async def recover_to_timestamp(
    repo: BackupRepository,
    event_source: EventSource,
    timestamp: float,
    *,
    event_sink: EventSink,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    projector: Projector,
    dry_run: bool = False,
    require_assets: bool = True,
) -> tuple[RecoveryTarget, RestorePlan, RestoreResult | None]:
    """Recover state to wall-clock ``timestamp`` (PITR by time)."""
    target = await resolve_target_timestamp(repo, event_source, timestamp)
    plan, result = await restore(
        repo,
        target.head_id,
        event_sink=event_sink,
        canon=canon,
        read_models=read_models,
        assets=assets,
        through=target.replay_through,
        projector=projector,
        dry_run=dry_run,
        require_assets=require_assets,
    )
    logger.info(
        "dr.pitr.timestamp",
        timestamp=timestamp,
        head_id=target.head_id,
        bound=target.replay_through,
        dry_run=dry_run,
    )
    return target, plan, result


__all__ = [
    "RecoveryTarget",
    "recover_to_position",
    "recover_to_timestamp",
    "resolve_target_position",
    "resolve_target_timestamp",
]
