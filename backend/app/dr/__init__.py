"""Disaster recovery — consistent backup & restore for Kinora's state of record.

Kinora's *state of record* is three stores, and losing any of them is
catastrophic (AGENTS.md): the **canon** (versioned entities + episodic shot
records, kinora.md §8), the **event store** (the append-only log of facts +
projection checkpoints, kinora.md §6/§9.7), the materialised **read models** the
event log folds into, and the **object-store assets** (rendered clips, narration
audio, keyframes, locked references) those records point at.

This package builds a coordinated **point-in-time backup & restore** engine over
those stores — *additively*, behind injectable interfaces, with deterministic
in-memory fakes so the whole thing is unit-tested with **no infra and zero
spend**. Nothing here writes through the live render/spend path.

The shape, facet by facet (every module is pure given its injected collaborators):

* :mod:`app.dr.interfaces` — the injectable **source/sink seams**: a canon
  source, an event-store reader (positions + replay), a read-model store, an
  object-store asset source, and the **backup repository** the snapshots land in.
  Each has a deterministic in-memory fake the tests drive.
* :mod:`app.dr.models` — the pydantic v2 **wire models**: a snapshot descriptor
  (the pinned event position + the tier + the parent chain), a per-segment
  manifest (canon / events / checkpoints / read-models / asset-manifest) with
  per-segment checksums, the backup-set **manifest**, the RPO/RTO accounting
  record, and the backup **health report**.
* :mod:`app.dr.checksums` — canonical, order-independent serialisation +
  SHA-256 **integrity checksums** so a single flipped byte is caught on restore.
* :mod:`app.dr.snapshot` — the **consistent snapshot engine**: pin the event
  store's head position *first*, then capture canon / checkpoints / read-models /
  the asset manifest **as of** that position so the snapshot is internally
  consistent (no asset referenced by a captured shot is missing, no event past
  the pin leaks in).
* :mod:`app.dr.tiers` — **full + incremental** backup tiers: a full snapshot is
  self-contained; an incremental captures only events past its parent's pin (+ a
  read-model/canon delta) and forms a restore **chain** back to a full.
* :mod:`app.dr.retention` — a tiered **retention policy** (keep-N-full,
  keep-incrementals-since-last-full, grandfather-father-son age tiers) + a
  **GC** that never orphans an incremental's parent.
* :mod:`app.dr.manifest` — assembles + **verifies** a backup-set manifest
  (checksum recomputation, chain completeness, asset-manifest/canon coherence).
* :mod:`app.dr.restore` — the **restore engine**: replay the event log to the
  pinned position into a (clean) target event store, **rebuild** the read models
  by re-projecting, and **verify** every referenced asset is present — with a
  **dry-run** (verify-only, mutate nothing) and a post-restore verification pass.
* :mod:`app.dr.pitr` — **point-in-time recovery**: resolve a target position or
  timestamp ``T`` to a ``(snapshot, replay-bound)`` plan over the chain, then
  restore exactly to ``T``.
* :mod:`app.dr.accounting` — **RPO/RTO** math (data-loss window vs. recovery
  time against the configured objectives) + the backup **health report**
  (coverage, freshness, chain integrity, objective compliance).
* :mod:`app.dr.service` — the orchestrating **facade** tying the seams together:
  ``backup_full`` / ``backup_incremental`` / ``restore`` / ``recover_to`` /
  ``health_report`` / ``gc``.
* :mod:`app.dr.config` — an additive, pure :class:`DRConfig` (objectives +
  retention knobs) with safe defaults; nothing is read from the network.

Everything is import-safe: no sockets, no DB, no event loop at import time.
"""

from __future__ import annotations

__all__: list[str] = [
    "accounting",
    "checksums",
    "config",
    "errors",
    "interfaces",
    "manifest",
    "models",
    "pitr",
    "restore",
    "retention",
    "service",
    "snapshot",
    "tiers",
]
