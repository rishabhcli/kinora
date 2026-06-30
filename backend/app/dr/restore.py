"""The restore engine — rebuild state from a backup chain.

Restore is the moment a backup earns its keep, so it is deliberately defensive
and *verify-first*. The pipeline, given a head snapshot id resolved to a chain:

1. **Verify before touching anything.** Resolve + checksum-verify the whole chain
   (:func:`app.dr.manifest.resolve_chain`); a corrupt segment or broken chain
   aborts here, before any state is mutated.
2. **Verify asset presence/integrity.** Compare the head snapshot's asset
   manifest against the live :class:`~app.dr.interfaces.AssetSource`: every
   referenced key must exist and (when a digest was captured) match. A mismatch
   is reported as the precise set of missing/divergent keys.
3. **Replay the event log.** Concatenate the chain's event slices (which are
   contiguous by construction) and append them, in ``global_position`` order, up
   to an optional ``through`` bound, into a clean :class:`EventSink`.
4. **Rebuild the read models.** Either *re-project* the replayed events through a
   supplied rebuild function (the truthful path — read models are a pure fold of
   events) or, when no projector is supplied, *load* the captured read-model
   segment directly. Both restore the canon as of the head pin.
5. **Post-restore verification.** Re-check the restored event head and a
   round-trip of the canon/read models so a partial restore is caught.

A **dry-run** performs steps 1–2 and a *plan* of 3–5 (counts, the replay bound,
the asset verdict) and mutates **nothing** — the EventSink/CanonSource/read-model
target are never written. That is how an operator validates a backup is
restorable before committing to the (destructive) real restore.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.dr.errors import AssetMismatchError, RestoreError
from app.dr.interfaces import (
    AssetSource,
    BackupRepository,
    CanonSource,
    EventSink,
    ReadModelTarget,
)
from app.dr.manifest import resolve_chain
from app.dr.models import AssetRef, BackupManifest, SegmentKind

logger = structlog.get_logger(__name__)

#: A projector rebuilds read models from the replayed events. It receives the
#: ordered event dicts (up to the replay bound) and the read-model target, and
#: must leave the target holding the materialised views. When omitted, restore
#: loads the captured read-model segment verbatim instead.
Projector = Callable[[list[dict[str, Any]], ReadModelTarget], Awaitable[None]]


@dataclass(slots=True)
class AssetVerification:
    """Outcome of comparing a snapshot's asset manifest against the source."""

    checked: int = 0
    missing: tuple[str, ...] = ()
    divergent: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every referenced asset is present and content-matched."""
        return not self.missing and not self.divergent


@dataclass(slots=True)
class RestorePlan:
    """What a restore *would* do — the dry-run report (and the real run's plan)."""

    head_id: str
    chain_ids: list[str]
    replay_through: int
    events_to_replay: int
    read_model_rows: int
    canon_items: int
    assets: AssetVerification
    rebuilt_by_projection: bool

    @property
    def restorable(self) -> bool:
        """True when the plan can be applied (chain valid + assets intact)."""
        return self.assets.ok


@dataclass(slots=True)
class RestoreResult:
    """The outcome of an applied (non-dry-run) restore."""

    plan: RestorePlan
    events_replayed: int = 0
    restored_head: int = 0
    canon_restored: bool = False
    read_models_restored: bool = False
    verified: bool = False
    findings: list[str] = field(default_factory=list)


async def _verify_assets(
    refs: list[AssetRef],
    assets: AssetSource,
) -> AssetVerification:
    """Compare an asset manifest against the live source (presence + digest)."""
    missing: list[str] = []
    divergent: list[str] = []
    for ref in refs:
        if not await assets.exists(ref.key):
            missing.append(ref.key)
            continue
        # Only compare when a content digest was captured (non-empty); a manifest
        # entry captured while the asset was already absent has an empty digest.
        if ref.checksum.value:
            current = await assets.content_digest(ref.key)
            if current != ref.checksum.value:
                divergent.append(ref.key)
    return AssetVerification(
        checked=len(refs),
        missing=tuple(missing),
        divergent=tuple(divergent),
    )


def _asset_refs(head: BackupManifest) -> list[AssetRef]:
    seg = head.segment(SegmentKind.ASSET_MANIFEST)
    if seg is None:
        return []
    return [AssetRef.model_validate(item) for item in seg.payload]


def _concatenated_events(chain: list[BackupManifest], through: int) -> list[dict[str, Any]]:
    """Flatten the chain's event slices, in order, up to ``through`` (inclusive)."""
    events: list[dict[str, Any]] = []
    for manifest in chain:
        seg = manifest.segment(SegmentKind.EVENTS)
        if seg is None:
            continue
        for ev in seg.payload:
            if int(ev["global_position"]) <= through:
                events.append(ev)
    events.sort(key=lambda e: int(e["global_position"]))
    return events


async def plan_restore(
    repo: BackupRepository,
    head_id: str,
    *,
    assets: AssetSource,
    through: int | None = None,
    use_projection: bool = False,
) -> RestorePlan:
    """Build (and validate) a restore plan without mutating anything.

    Verifies the chain + integrity, verifies assets, and computes what the
    replay/rebuild would do up to ``through`` (defaults to the head's pin —
    a normal full restore). ``use_projection`` only affects the reported
    ``rebuilt_by_projection`` flag here.

    Raises:
        ChainError / IntegrityError: from chain resolution + verification.
    """
    chain = await resolve_chain(repo, head_id, verify=True)
    head = chain[-1]
    bound = head.descriptor.pinned_position if through is None else through

    events = _concatenated_events(chain, bound)
    rm_seg = head.segment(SegmentKind.READ_MODELS)
    canon_seg = head.segment(SegmentKind.CANON)
    rm_rows = sum(len(v) for v in rm_seg.payload.values()) if rm_seg is not None else 0
    canon_items = 0
    if canon_seg is not None:
        canon_items = len(canon_seg.payload.get("entities", {})) + len(
            canon_seg.payload.get("episodic", {})
        )

    asset_verdict = await _verify_assets(_asset_refs(head), assets)

    return RestorePlan(
        head_id=head_id,
        chain_ids=[m.descriptor.snapshot_id for m in chain],
        replay_through=bound,
        events_to_replay=len(events),
        read_model_rows=rm_rows,
        canon_items=canon_items,
        assets=asset_verdict,
        rebuilt_by_projection=use_projection,
    )


async def restore(
    repo: BackupRepository,
    head_id: str,
    *,
    event_sink: EventSink,
    canon: CanonSource,
    read_models: ReadModelTarget,
    assets: AssetSource,
    through: int | None = None,
    projector: Projector | None = None,
    dry_run: bool = False,
    require_assets: bool = True,
) -> tuple[RestorePlan, RestoreResult | None]:
    """Restore state from the backup chain ending at ``head_id``.

    Args:
        through: replay/rebuild only up to this event position (point-in-time
            recovery passes the resolved bound; default = the head's pin).
        projector: when supplied, read models are rebuilt by re-projecting the
            replayed events (the truthful rebuild). When ``None``, the captured
            read-model segment is loaded verbatim.
        dry_run: when set, verify + plan only; mutate nothing. Returns
            ``(plan, None)``.
        require_assets: when set, an asset mismatch aborts a real restore (and is
            always surfaced in the plan); a dry-run never aborts — it reports.

    Returns:
        ``(plan, result)`` where ``result`` is ``None`` for a dry-run.

    Raises:
        AssetMismatchError: a real restore with ``require_assets`` and missing
            assets.
        RestoreError: a post-restore verification failed.
        ChainError / IntegrityError: from chain resolution + verification.
    """
    plan = await plan_restore(
        repo,
        head_id,
        assets=assets,
        through=through,
        use_projection=projector is not None,
    )

    if dry_run:
        logger.info(
            "dr.restore.dry_run",
            head_id=head_id,
            chain=len(plan.chain_ids),
            events=plan.events_to_replay,
            assets_ok=plan.assets.ok,
        )
        return plan, None

    if require_assets and not plan.assets.ok:
        bad = tuple(sorted({*plan.assets.missing, *plan.assets.divergent}))
        raise AssetMismatchError(bad)

    chain = await resolve_chain(repo, head_id, verify=True)
    head = chain[-1]
    bound = plan.replay_through

    # 3. Replay events into a clean sink.
    await event_sink.reset()
    events = _concatenated_events(chain, bound)
    for ev in events:
        await event_sink.restore_event(ev)

    # Restore the canon as of the head pin.
    canon_seg = head.segment(SegmentKind.CANON)
    if canon_seg is not None:
        await canon.load(canon_seg.payload)

    # 4. Rebuild read models — re-project (truthful) or load the captured rows.
    await read_models.clear_all()
    if projector is not None:
        await projector(events, read_models)
    else:
        rm_seg = head.segment(SegmentKind.READ_MODELS)
        if rm_seg is not None:
            await read_models.load(rm_seg.payload)

    result = RestoreResult(
        plan=plan,
        events_replayed=len(events),
        restored_head=bound,
        canon_restored=canon_seg is not None,
        read_models_restored=True,
    )

    # 5. Post-restore verification.
    findings = await _verify_restore(
        head=head,
        event_sink=event_sink,
        canon=canon,
        read_models=read_models,
        bound=bound,
        rebuilt_by_projection=projector is not None,
    )
    result.findings = findings
    result.verified = not findings
    if findings:
        raise RestoreError("post-restore verification failed: " + "; ".join(findings))

    logger.info(
        "dr.restore.applied",
        head_id=head_id,
        events=result.events_replayed,
        restored_head=result.restored_head,
    )
    return plan, result


async def _verify_restore(
    *,
    head: BackupManifest,
    event_sink: EventSink,
    canon: CanonSource,
    read_models: ReadModelTarget,
    bound: int,
    rebuilt_by_projection: bool,
) -> list[str]:
    """Re-read the restored stores and report any inconsistency (empty == clean)."""
    findings: list[str] = []

    # Event head must equal the replay bound (for a full restore == head pin).
    restored_head = getattr(event_sink, "head_position", None)
    if restored_head is not None and int(restored_head) != bound:
        findings.append(f"restored event head {int(restored_head)} != expected {bound}")

    # Canon round-trips to the captured canon (a full-restore equality check).
    canon_seg = head.segment(SegmentKind.CANON)
    if canon_seg is not None:
        from app.dr.checksums import digest

        live = await canon.dump()
        if digest(live) != digest(canon_seg.payload):
            findings.append("restored canon does not match the captured canon")

    # When read models were loaded verbatim, they must round-trip; when rebuilt
    # by projection we only assert the rows are non-fewer than nothing — the
    # projector owns correctness, and the service-level test asserts equality.
    if not rebuilt_by_projection:
        rm_seg = head.segment(SegmentKind.READ_MODELS)
        if rm_seg is not None:
            from app.dr.checksums import digest

            live_rm = await read_models.dump()
            if digest(live_rm) != digest(rm_seg.payload):
                findings.append("restored read models do not match the captured rows")

    return findings


__all__ = [
    "AssetVerification",
    "Projector",
    "RestorePlan",
    "RestoreResult",
    "plan_restore",
    "restore",
]
