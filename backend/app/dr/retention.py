"""Retention policy + garbage collection over the backup fleet.

A backup fleet grows without bound unless something prunes it, but pruning is
dangerous: an incremental is *useless without its whole parent chain*, so a GC
that deletes a full while keeping its incrementals would silently destroy
recoverability. The invariant this module enforces is therefore:

    **Never delete a backup that any retained backup's chain depends on.**

The policy (:class:`~app.dr.config.DRConfig`) is a grandfather-father-son style:

* keep the ``keep_full`` most-recent full backups (a full and *all* its
  incrementals form one retainable chain);
* additionally keep the incrementals of the ``keep_incremental_chains`` most
  recent chains (older chains are retired whole — full + its incrementals);
* never collect anything younger than ``min_retain_age_s`` (a freshness floor).

:func:`plan_gc` computes the *set to delete* without touching the repository and
proves the no-orphan invariant on the survivors; :func:`run_gc` applies it. The
plan is deterministic given the fleet + a ``now`` clock (injected, never read).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from app.dr.config import DRConfig
from app.dr.errors import RetentionError
from app.dr.interfaces import BackupRepository
from app.dr.models import BackupManifest, BackupTier

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class GCPlan:
    """The computed retention decision (what to keep / drop), pre-application."""

    keep: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    #: full snapshot id -> its retained chain length, for the report.
    retained_chains: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)


def _chain_root(manifest: BackupManifest, by_id: dict[str, BackupManifest]) -> str | None:
    """Walk parents to the founding full's id; ``None`` if the chain is broken."""
    cursor: str | None = manifest.descriptor.snapshot_id
    seen: set[str] = set()
    while cursor is not None:
        if cursor in seen:
            return None
        seen.add(cursor)
        m = by_id.get(cursor)
        if m is None:
            return None
        if m.descriptor.tier is BackupTier.FULL:
            return cursor
        cursor = m.descriptor.parent_id
    return None


def plan_gc(
    manifests: list[BackupManifest],
    config: DRConfig,
    *,
    now: datetime,
) -> GCPlan:
    """Compute which backups to retain vs. collect, preserving every chain.

    Args:
        manifests: every backup currently in the fleet.
        config: the retention policy.
        now: the reference clock for age decisions (injected; never read here).

    Returns:
        A :class:`GCPlan`. Applying it never orphans a retained incremental.
    """
    by_id = {m.descriptor.snapshot_id: m for m in manifests}
    plan = GCPlan()

    # Group every backup under its chain root (the founding full).
    chains: dict[str, list[BackupManifest]] = {}
    orphans: list[BackupManifest] = []
    for m in manifests:
        root = _chain_root(m, by_id)
        if root is None:
            orphans.append(m)
        else:
            chains.setdefault(root, []).append(m)

    # Order chains by their full's recency (newest first).
    full_by_root = {
        root: by_id[root] for root in chains if by_id[root].descriptor.tier is BackupTier.FULL
    }
    ordered_roots = sorted(
        full_by_root,
        key=lambda r: full_by_root[r].descriptor.created_at,
        reverse=True,
    )

    # The most-recent ``keep_full`` chains are kept *whole* (full + incrementals).
    keep_full_roots = set(ordered_roots[: config.keep_full])
    # Among the kept-full chains, the most recent ``keep_incremental_chains`` keep
    # their incrementals too; the rest keep only the full (incrementals retired).
    keep_inc_roots = set(ordered_roots[: config.keep_incremental_chains])

    for root in ordered_roots:
        members = chains[root]
        chain_len = 0
        for m in members:
            sid = m.descriptor.snapshot_id
            is_full = m.descriptor.tier is BackupTier.FULL
            age_s = (now - m.descriptor.created_at).total_seconds()
            too_young = age_s < config.min_retain_age_s

            keep_this: bool
            reason: str
            if root in keep_full_roots and is_full:
                keep_this, reason = True, "recent-full"
            elif root in keep_inc_roots and not is_full:
                keep_this, reason = True, "recent-chain-incremental"
            elif root in keep_full_roots and not is_full:
                # Full is retained but this chain is beyond keep_incremental_chains:
                # the incremental is collectible *unless* it is younger than the
                # freshness floor.
                keep_this, reason = (
                    (True, "freshness-floor")
                    if too_young
                    else (
                        False,
                        "stale-incremental",
                    )
                )
            else:
                # Whole chain is beyond keep_full → collect the full + incrementals,
                # respecting the freshness floor.
                keep_this, reason = (
                    (True, "freshness-floor")
                    if too_young
                    else (
                        False,
                        "retired-chain",
                    )
                )

            if keep_this:
                plan.keep.append(sid)
                if not is_full:
                    chain_len += 1
            else:
                plan.delete.append(sid)
            plan.reasons[sid] = reason
        if root in keep_full_roots:
            plan.retained_chains[root] = chain_len

    # Orphans (broken/forged lineage): never auto-delete — surface for an operator.
    for m in orphans:
        sid = m.descriptor.snapshot_id
        plan.keep.append(sid)
        plan.reasons[sid] = "orphan-kept-for-review"

    _assert_no_orphans(plan, by_id)
    return plan


def _assert_no_orphans(plan: GCPlan, by_id: dict[str, BackupManifest]) -> None:
    """Fail loudly if applying ``plan`` would orphan a retained incremental."""
    deleted = set(plan.delete)
    kept = set(plan.keep)
    for sid in kept:
        m = by_id[sid]
        if m.descriptor.tier is BackupTier.INCREMENTAL:
            # Every ancestor up to the full must survive.
            cursor = m.descriptor.parent_id
            seen: set[str] = set()
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                if cursor in deleted:
                    raise RetentionError(
                        f"GC plan would orphan retained incremental {sid!r}: "
                        f"its ancestor {cursor!r} is scheduled for deletion"
                    )
                parent = by_id.get(cursor)
                cursor = parent.descriptor.parent_id if parent is not None else None


async def run_gc(
    repo: BackupRepository,
    config: DRConfig,
    *,
    now: datetime,
) -> GCPlan:
    """Compute + apply retention against ``repo``; return the executed plan."""
    manifests: list[BackupManifest] = []
    for sid in await repo.list_ids():
        m = await repo.get(sid)
        if m is not None:
            manifests.append(m)

    plan = plan_gc(manifests, config, now=now)
    for sid in plan.delete:
        await repo.delete(sid)

    logger.info(
        "dr.gc.applied",
        kept=len(plan.keep),
        deleted=len(plan.delete),
        chains=len(plan.retained_chains),
    )
    return plan


__all__ = ["GCPlan", "plan_gc", "run_gc"]
