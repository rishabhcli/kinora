"""Pydantic v2 wire models for the backup format.

These are the *serialised shape* of everything the engine produces and consumes:
the captured segments, the per-snapshot manifest, the snapshot descriptor (the
pinned event position + tier + parent chain), and the reporting records (RPO/RTO
accounting, the backup health report). They are JSON-round-trippable so a
snapshot can be persisted by any :class:`~app.dr.interfaces.BackupRepository`
implementation (the in-memory fake, or a future object-store-backed one).

Design notes:

* A backup is a sequence of named **segments**; each :class:`Segment` carries its
  JSON payload *and* its :class:`~app.dr.checksums.Checksum` so integrity is
  verifiable without re-deriving the payload from elsewhere.
* The **event slice** segment is the spine of point-in-time correctness: it
  carries events ``(from_position, to_position]`` so a full snapshot pins
  ``from_position == 0`` and an incremental pins it to its parent's
  ``to_position`` — restore replays the concatenation of a chain's slices.
* The **asset manifest** segment lists every object-store key the captured state
  references plus that asset's own checksum/size, so restore can verify asset
  *presence and integrity* without copying bytes into the backup (assets stay in
  object storage; the backup records the manifest that must match).
* All timestamps are timezone-aware UTC; positions are the event store's
  gap-free ``global_position`` (0 == before the first event).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.dr.checksums import ALGORITHM


class BackupTier(enum.StrEnum):
    """The two backup tiers (kinora.md disaster-recovery posture)."""

    #: A self-contained snapshot: full canon + every event ``(0, head]`` + every
    #: read model + the full asset manifest. Restorable with no parent.
    FULL = "full"
    #: A delta against a parent: only events past the parent's pin, plus the
    #: canon/read-model/asset-manifest *state* as of the new pin. Restorable only
    #: as the tail of a chain ending in a full.
    INCREMENTAL = "incremental"


class SegmentKind(enum.StrEnum):
    """The logical contents of a backup segment."""

    CANON = "canon"
    EVENTS = "events"
    CHECKPOINTS = "checkpoints"
    READ_MODELS = "read_models"
    ASSET_MANIFEST = "asset_manifest"


class ChecksumModel(BaseModel):
    """The serialised form of a :class:`~app.dr.checksums.Checksum`."""

    model_config = ConfigDict(frozen=True)

    algorithm: str = ALGORITHM
    value: str

    def as_str(self) -> str:
        """A compact ``"algorithm:hex"`` string."""
        return f"{self.algorithm}:{self.value}"


class AssetRef(BaseModel):
    """One object-store asset the captured state references.

    ``checksum`` is the asset's *content* digest as observed at capture; restore
    compares the source's current digest against it to detect a silently-mutated
    or truncated asset (not just a missing key). ``size`` is advisory metadata.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    checksum: ChecksumModel
    size: int = 0
    #: Optional provenance: which captured record first referenced this asset
    #: (e.g. a ``shot_id``) — purely informational, aids triage on a mismatch.
    referenced_by: str | None = None


class Segment(BaseModel):
    """A named, checksummed unit of backup payload."""

    kind: SegmentKind
    #: The JSON payload. Shape depends on ``kind`` (a list of events for
    #: ``EVENTS``; a mapping for ``CANON``/``READ_MODELS``/``CHECKPOINTS``; a
    #: list of :class:`AssetRef` dicts for ``ASSET_MANIFEST``).
    payload: Any
    checksum: ChecksumModel
    #: Number of logical items (events, entities, rows, assets) — for the report.
    item_count: int = 0


class SnapshotDescriptor(BaseModel):
    """Identity + lineage of one snapshot (the manifest header)."""

    snapshot_id: str
    tier: BackupTier
    #: The event-store position this snapshot is consistent *as of*. Everything
    #: captured (canon, read models, assets) reflects the world at this position.
    pinned_position: int
    #: For an incremental, the position its event slice starts *after* (its
    #: parent's ``pinned_position``); 0 for a full.
    base_position: int
    #: For an incremental, the parent snapshot id; ``None`` for a full.
    parent_id: str | None = None
    created_at: datetime
    #: A roll-up checksum over all segment checksums (a quick whole-snapshot id).
    content_hash: ChecksumModel
    #: Free-form labels (e.g. ``{"env": "prod", "trigger": "scheduled"}``).
    labels: dict[str, str] = Field(default_factory=dict)


class BackupManifest(BaseModel):
    """A complete backup: its descriptor + every segment, self-verifiable.

    This is the persisted unit. :func:`app.dr.manifest.verify_manifest`
    recomputes every segment checksum and the roll-up so a tampered or bit-rotted
    backup is rejected before any restore touches it.
    """

    descriptor: SnapshotDescriptor
    segments: list[Segment]
    #: Format version so a future on-disk format change is detectable.
    format_version: int = 1

    def segment(self, kind: SegmentKind) -> Segment | None:
        """Return the segment of ``kind`` (the first, if any) or ``None``."""
        for seg in self.segments:
            if seg.kind == kind:
                return seg
        return None

    @property
    def total_items(self) -> int:
        """Sum of every segment's ``item_count`` (for size/coverage reporting)."""
        return sum(s.item_count for s in self.segments)


class RPORTOReport(BaseModel):
    """RPO/RTO accounting for one (real or simulated) recovery.

    * **RPO** (Recovery Point Objective) — the *data-loss window*: how far behind
      "now" the recovered point is. Here ``rpo_s`` is the wall-clock gap between
      the latest event in the source at recovery time and the event the recovery
      point lands on (smaller is better).
    * **RTO** (Recovery Time Objective) — the *recovery duration*: how long the
      restore took (``rto_s``), against the configured target.
    """

    recovery_point: int
    source_head: int
    rpo_s: float
    rpo_target_s: float
    rpo_met: bool
    rto_s: float
    rto_target_s: float
    rto_met: bool
    #: Number of events that would be lost (source_head - recovery_point), ≥ 0.
    events_lost: int = 0


class BackupHealth(BaseModel):
    """The backup-fleet health report (the operator dashboard payload)."""

    total_backups: int
    full_backups: int
    incremental_backups: int
    #: Most recent snapshot's age in seconds (vs. now); ``None`` if no backups.
    latest_backup_age_s: float | None
    #: The freshest recoverable RPO available right now (s); ``None`` if none.
    achievable_rpo_s: float | None
    #: Whether every chain is complete + every segment checksum verifies.
    integrity_ok: bool
    #: Whether the freshest achievable RPO meets the configured objective.
    rpo_objective_met: bool
    #: Per-chain summaries (head snapshot id -> chain length) for triage.
    chains: dict[str, int] = Field(default_factory=dict)
    #: Human-readable findings (e.g. "backup overdue", "chain c1 broken").
    findings: list[str] = Field(default_factory=list)


__all__ = [
    "AssetRef",
    "BackupHealth",
    "BackupManifest",
    "BackupTier",
    "ChecksumModel",
    "RPORTOReport",
    "Segment",
    "SegmentKind",
    "SnapshotDescriptor",
]
