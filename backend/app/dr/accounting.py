"""RPO/RTO accounting + the backup-fleet health report.

Two operator questions, answered as pure functions over the fleet + a clock:

* **"If we recovered right now, how much would we lose and how long would it
  take?"** — :func:`rpo_rto_report` turns a recovery point + a measured restore
  duration into an :class:`~app.dr.models.RPORTOReport`: the **RPO** (data-loss
  window — the wall-clock gap between the source's latest event and the recovered
  point) and the **RTO** (the restore duration), each compared to the configured
  objective so the verdict is a boolean, not a number to eyeball.

* **"Is the backup posture healthy?"** — :func:`health_report` summarises the
  whole fleet: counts by tier, the freshest snapshot's age, the *achievable*
  RPO right now (how stale the freshest recoverable point is), whether every
  chain resolves + every checksum verifies, and whether the achievable RPO meets
  the objective — with human-readable findings for anything amiss.

The RPO is measured in the same unit the in-memory event source uses for
``recorded_at`` (epoch seconds), and "now" / measured durations are injected, so
the math is deterministic and unit-tested without a real clock.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from app.dr.checksums import Checksum
from app.dr.config import DRConfig
from app.dr.errors import DRError
from app.dr.interfaces import BackupRepository, EventSource
from app.dr.manifest import resolve_chain, verify_manifest
from app.dr.models import (
    BackupHealth,
    BackupManifest,
    BackupTier,
    RPORTOReport,
    SegmentKind,
)

logger = structlog.get_logger(__name__)


def rpo_rto_report(
    *,
    recovery_point: int,
    source_head: int,
    recovery_point_time: float,
    source_head_time: float,
    restore_duration_s: float,
    config: DRConfig,
) -> RPORTOReport:
    """Compute an RPO/RTO report for a (real or simulated) recovery.

    Args:
        recovery_point: the event position the recovery lands on.
        source_head: the source log's head position at recovery time.
        recovery_point_time: ``recorded_at`` (epoch s) of the recovery point.
        source_head_time: ``recorded_at`` (epoch s) of the source head event.
        restore_duration_s: measured/estimated restore wall-clock (the RTO).
        config: carries the objectives to gate against.

    Returns:
        A populated :class:`RPORTOReport` with ``rpo_met`` / ``rto_met`` verdicts.
    """
    rpo_s = max(0.0, source_head_time - recovery_point_time)
    events_lost = max(0, source_head - recovery_point)
    rto_s = max(0.0, restore_duration_s)
    return RPORTOReport(
        recovery_point=recovery_point,
        source_head=source_head,
        rpo_s=rpo_s,
        rpo_target_s=config.rpo_target_s,
        rpo_met=rpo_s <= config.rpo_target_s,
        rto_s=rto_s,
        rto_target_s=config.rto_target_s,
        rto_met=rto_s <= config.rto_target_s,
        events_lost=events_lost,
    )


def _verify_silently(manifest: BackupManifest) -> bool:
    """True iff ``manifest`` passes integrity verification (no raise)."""
    try:
        verify_manifest(manifest)
    except DRError:
        return False
    return True


def _freshest(manifests: list[BackupManifest]) -> BackupManifest | None:
    if not manifests:
        return None
    return max(manifests, key=lambda m: m.descriptor.created_at)


async def health_report(
    repo: BackupRepository,
    config: DRConfig,
    *,
    now: datetime,
    event_source: EventSource | None = None,
) -> BackupHealth:
    """Summarise the backup fleet's health (the operator dashboard payload).

    Args:
        repo: the snapshot vault.
        config: thresholds (overdue, RPO objective).
        now: reference clock (injected).
        event_source: optional live log; when supplied the achievable RPO is the
            wall-clock gap between the live head event time and the freshest
            backup's latest event time (a true data-loss window). Without it, the
            achievable RPO falls back to the freshest backup's *age*.

    Returns:
        A :class:`BackupHealth`.
    """
    manifests: list[BackupManifest] = []
    for sid in await repo.list_ids():
        m = await repo.get(sid)
        if m is not None:
            manifests.append(m)

    fulls = [m for m in manifests if m.descriptor.tier is BackupTier.FULL]
    incs = [m for m in manifests if m.descriptor.tier is BackupTier.INCREMENTAL]
    findings: list[str] = []

    # Integrity: every manifest checksum-verifies and every chain resolves.
    integrity_ok = all(_verify_silently(m) for m in manifests)
    if not integrity_ok:
        bad = [m.descriptor.snapshot_id for m in manifests if not _verify_silently(m)]
        findings.append(f"integrity check failed for: {', '.join(sorted(bad))}")

    # Resolve every chain head (an incremental that is no one's parent is a head).
    chains: dict[str, int] = {}
    head_ids = _chain_heads(manifests)
    for head_id in head_ids:
        try:
            chain = await resolve_chain(repo, head_id, verify=False)
            chains[head_id] = len(chain)
        except DRError as exc:
            integrity_ok = False
            findings.append(f"chain {head_id!r} broken: {exc}")

    freshest = _freshest(manifests)
    latest_age: float | None = None
    achievable_rpo: float | None = None
    rpo_met = False

    if freshest is not None:
        latest_age = (now - freshest.descriptor.created_at).total_seconds()
        if latest_age > config.overdue_after_s:
            findings.append(
                f"backup overdue: freshest is {latest_age:.0f}s old "
                f"(threshold {config.overdue_after_s:.0f}s)"
            )
        # Achievable RPO.
        if event_source is not None:
            head = await event_source.head_position()
            backup_latest_pos = freshest.descriptor.pinned_position
            head_time = await _position_time(event_source, head)
            backup_time = await _position_time(event_source, backup_latest_pos)
            if head_time is not None and backup_time is not None:
                achievable_rpo = max(0.0, head_time - backup_time)
            else:
                achievable_rpo = latest_age
        else:
            achievable_rpo = latest_age
        rpo_met = achievable_rpo <= config.rpo_target_s
        if not rpo_met:
            findings.append(
                f"achievable RPO {achievable_rpo:.0f}s exceeds objective "
                f"{config.rpo_target_s:.0f}s"
            )
    else:
        findings.append("no backups exist — the fleet is unprotected")

    if not fulls and manifests:
        findings.append("no full backup exists — incrementals are unrecoverable")
        integrity_ok = False

    return BackupHealth(
        total_backups=len(manifests),
        full_backups=len(fulls),
        incremental_backups=len(incs),
        latest_backup_age_s=latest_age,
        achievable_rpo_s=achievable_rpo,
        integrity_ok=integrity_ok,
        rpo_objective_met=rpo_met,
        chains=chains,
        findings=findings,
    )


def _chain_heads(manifests: list[BackupManifest]) -> list[str]:
    """Ids that are no other backup's parent (the tip of each chain)."""
    parents = {m.descriptor.parent_id for m in manifests if m.descriptor.parent_id is not None}
    return [m.descriptor.snapshot_id for m in manifests if m.descriptor.snapshot_id not in parents]


async def _position_time(event_source: EventSource, position: int) -> float | None:
    """Best-effort ``recorded_at`` for ``position`` via a 1-event range read."""
    if position <= 0:
        return 0.0
    events = await event_source.read_range(position - 1, position)
    if not events:
        return None
    return float(events[-1]["recorded_at"])


def snapshot_content_hash(manifest: BackupManifest) -> Checksum:
    """Recompute a manifest's roll-up content hash (a quick fleet de-dup key)."""
    from app.dr.checksums import combine

    seg = manifest.segment(SegmentKind.EVENTS)  # touch to assert well-formed
    if seg is None:  # pragma: no cover - guarded earlier
        raise DRError("manifest missing event segment")
    return combine(
        *(
            Checksum(algorithm=s.checksum.algorithm, value=s.checksum.value)
            for s in manifest.segments
        )
    )


__all__ = ["health_report", "rpo_rto_report", "snapshot_content_hash"]
