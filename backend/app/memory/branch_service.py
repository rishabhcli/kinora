"""Canon FORK / DIFF / MERGE over branches (kinora.md §8 + §7.2).

A director edit can be risky — re-casting a character, changing a location's fate. Rather
than mutate ``main`` in place, the editor **forks** a branch off a base coordinate, makes
its edits there, **diffs** it against main to preview the change, and **merges** it back.
Because every fact carries a CRDT write-stamp (:mod:`app.memory.crdt`), the merge is
conflict-free: genuinely-concurrent edits to the same fact resolve deterministically by the
last-writer-wins rule (higher HLC stamp), and the loser is recorded as a
:class:`~app.memory.contracts.MergeConflict` for the §7.2 negotiation surface — never
silently dropped.

The merge classifies into three strategies:

* **no_op** — the branches are identical (nothing to apply).
* **fast_forward** — only the source changed relative to the merge base; apply its edits.
* **merged** — both sides changed concurrently; apply the source edits, LWW-resolving any
  fact both sides touched, and report the conflicts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.db.models.bitemporal import AuditAction, BitemporalState, BranchStatus
from app.db.repositories.bitemporal import (
    BitemporalStateRepo,
    CanonBranchRepo,
)
from app.memory.audit_log import AuditLog
from app.memory.bitemporal import MAIN_BRANCH, utcnow
from app.memory.contracts import (
    BranchDiff,
    BranchInfo,
    FactChange,
    MergeConflict,
    MergeResult,
)
from app.memory.crdt import HLC, Stamp
from app.memory.temporal_state_service import TemporalStateService


class BranchError(RuntimeError):
    """Raised on an invalid branch operation (duplicate name, unknown branch, …)."""


def _current_by_key(rows: list[BitemporalState]) -> dict[str, BitemporalState]:
    """Map fact_key → its *current belief* (``tx_to IS NULL``) on a branch."""
    out: dict[str, BitemporalState] = {}
    for row in rows:
        if row.tx_to is None:
            out[row.fact_key] = row
    return out


def _is_active(row: BitemporalState) -> bool:
    """A current belief that has not been forgotten (valid interval still open)."""
    return row.valid_to_beat is None


class BranchService:
    """Fork a branch, diff two branches, three-way CRDT-merge one into another."""

    def __init__(
        self,
        states: BitemporalStateRepo,
        branches: CanonBranchRepo,
        audit: AuditLog,
        temporal: TemporalStateService,
        *,
        actor_id: str = "system",
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._states = states
        self._branches = branches
        self._audit = audit
        self._temporal = temporal
        self._actor = actor_id
        self._now = now

    # --- FORK --------------------------------------------------------------- #

    async def fork(
        self,
        *,
        book_id: str,
        name: str,
        base_beat: int | None = None,
        base_tx: datetime | None = None,
        parent: str = MAIN_BRANCH,
        note: str | None = None,
    ) -> BranchInfo:
        """Create a branch off ``(parent, base_beat, base_tx)`` and copy the base snapshot.

        The fork seeds the new branch with the parent's *active* facts as of the base
        coordinate so edits are diffed against a real common ancestor. The copies carry the
        same fact_key + a fresh tx interval on the new branch — so divergence is visible per
        fact at merge time.
        """
        await self._states.advisory_lock(book_id, name)
        existing = await self._branches.get(book_id, name)
        if existing is not None:
            raise BranchError(f"branch already exists: {name}")
        if name == MAIN_BRANCH:
            raise BranchError("cannot fork onto 'main'")

        info = await self._branches.create(
            book_id=book_id,
            name=name,
            parent=parent,
            base_beat=base_beat,
            base_tx=base_tx,
            note=note,
        )
        # Seed the branch with the parent's active facts at the base coordinate.
        from app.memory.bitemporal import LATEST_BEAT

        beat = LATEST_BEAT if base_beat is None else base_beat
        base_rows = await self._states.as_of(book_id, parent, beat, base_tx)
        now = self._now()
        for row in base_rows:
            if not _is_active(row):
                continue
            await self._states.insert(
                book_id=book_id,
                fact_key=row.fact_key,
                branch=name,
                subject_entity_key=row.subject_entity_key,
                predicate=row.predicate,
                object_value=row.object_value,
                valid_from_beat=row.valid_from_beat,
                valid_to_beat=row.valid_to_beat,
                tx_from=now,
                stamp_wall=row.stamp_wall,
                stamp_counter=row.stamp_counter,
                actor_id=row.actor_id,
                source_span=row.source_span,
            )
        await self._audit.record(
            book_id=book_id,
            branch=name,
            action=AuditAction.FORK_BRANCH,
            actor_id=self._actor,
            target_key=name,
            payload={"parent": parent, "base_beat": base_beat, "seeded": len(base_rows)},
        )
        return _to_branch_info(info)

    # --- DIFF --------------------------------------------------------------- #

    async def diff(self, *, book_id: str, branch_a: str, branch_b: str) -> BranchDiff:
        """The structural difference between the *current beliefs* of two branches.

        Reports, per logical fact: added (only in B), removed (only in A), changed (different
        object), retired (B forgot a fact A still holds). Diffs A→B (B is "theirs").
        """
        a = _current_by_key(await self._states.list_branch(book_id, branch_a))
        b = _current_by_key(await self._states.list_branch(book_id, branch_b))
        changes: list[FactChange] = []

        for key in sorted(set(a) | set(b)):
            ra, rb = a.get(key), b.get(key)
            if ra is None and rb is not None:
                changes.append(_change(rb, "added", before=None, after=rb.object_value))
            elif ra is not None and rb is None:
                changes.append(_change(ra, "removed", before=ra.object_value, after=None))
            elif ra is not None and rb is not None:
                if ra.object_value != rb.object_value:
                    changes.append(
                        _change(rb, "changed", before=ra.object_value, after=rb.object_value)
                    )
                elif _is_active(ra) and not _is_active(rb):
                    changes.append(
                        _change(rb, "retired", before=ra.object_value, after=rb.object_value)
                    )
        return BranchDiff(
            book_id=book_id, branch_a=branch_a, branch_b=branch_b, changes=changes
        )

    # --- MERGE -------------------------------------------------------------- #

    async def merge(
        self, *, book_id: str, source: str, target: str = MAIN_BRANCH
    ) -> MergeResult:
        """Three-way CRDT-merge ``source`` into ``target`` (LWW on concurrent edits).

        For each fact the source changed relative to the target's belief, apply it; when the
        target *also* changed that fact concurrently, the higher CRDT stamp wins and the loss
        is reported as a conflict (the §7.2 surface). Branch is marked ``merged`` on success.
        """
        await self._states.advisory_lock(book_id, target)
        src_branch = await self._branches.get(book_id, source)
        if src_branch is None and source != MAIN_BRANCH:
            raise BranchError(f"unknown source branch: {source}")

        src = _current_by_key(await self._states.list_branch(book_id, source))
        tgt = _current_by_key(await self._states.list_branch(book_id, target))

        applied = 0
        conflicts: list[MergeConflict] = []
        merged_facts: list[str] = []

        for key, srow in src.items():
            trow = tgt.get(key)
            if trow is None:
                # New fact on the source — bring it over (assert on target).
                await self._copy_into(book_id, target, srow)
                applied += 1
                merged_facts.append(key)
                continue
            if srow.object_value == trow.object_value and _is_active(srow) == _is_active(trow):
                continue  # identical belief, nothing to do
            # Both branches hold this fact with different beliefs → LWW by CRDT stamp.
            s_stamp = Stamp(HLC(srow.stamp_wall, srow.stamp_counter), srow.actor_id)
            t_stamp = Stamp(HLC(trow.stamp_wall, trow.stamp_counter), trow.actor_id)
            if s_stamp > t_stamp:
                await self._temporal.correct_fact(
                    book_id=book_id,
                    fact_key=key,
                    new_object=srow.object_value,
                    branch=target,
                )
                applied += 1
                merged_facts.append(key)
                conflicts.append(
                    _conflict(srow, trow, winner="source", reason="source stamp dominates")
                )
            else:
                conflicts.append(
                    _conflict(srow, trow, winner="target", reason="target stamp dominates")
                )

        strategy = self._classify(src, tgt, applied, conflicts)
        if source != MAIN_BRANCH and src_branch is not None:
            await self._branches.set_status(book_id, source, BranchStatus.MERGED)
        await self._audit.record(
            book_id=book_id,
            branch=target,
            action=AuditAction.MERGE_BRANCH,
            actor_id=self._actor,
            target_key=source,
            payload={"strategy": strategy, "applied": applied, "conflicts": len(conflicts)},
        )
        return MergeResult(
            book_id=book_id,
            source=source,
            target=target,
            strategy=strategy,
            applied=applied,
            conflicts=conflicts,
            merged_facts=merged_facts,
        )

    async def list_branches(self, book_id: str) -> list[BranchInfo]:
        rows = await self._branches.list_for_book(book_id)
        return [_to_branch_info(r) for r in rows]

    # --- internals ---------------------------------------------------------- #

    async def _copy_into(self, book_id: str, target: str, srow: BitemporalState) -> None:
        """Bring a source-only fact onto the target branch as a fresh current belief."""
        await self._temporal.assert_fact(
            book_id=book_id,
            subject_entity_key=srow.subject_entity_key,
            predicate=srow.predicate,
            object_value=srow.object_value,
            valid_from_beat=srow.valid_from_beat,
            branch=target,
            fact_key=srow.fact_key,
            source_span=srow.source_span,
        )

    @staticmethod
    def _classify(
        src: dict[str, BitemporalState],
        tgt: dict[str, BitemporalState],
        applied: int,
        conflicts: list[MergeConflict],
    ) -> str:
        if applied == 0 and not conflicts:
            return "no_op"
        # If the target had no belief the source diverged from, it's a fast-forward.
        diverged_in_target = any(c.winner == "target" for c in conflicts)
        return "merged" if (conflicts or diverged_in_target) else "fast_forward"


def _change(
    row: BitemporalState, change: str, *, before: str | None, after: str | None
) -> FactChange:
    return FactChange(
        fact_key=row.fact_key,
        subject_entity_key=row.subject_entity_key,
        predicate=row.predicate,
        change=change,
        object_before=before,
        object_after=after,
    )


def _conflict(
    srow: BitemporalState, trow: BitemporalState, *, winner: str, reason: str
) -> MergeConflict:
    return MergeConflict(
        fact_key=srow.fact_key,
        subject_entity_key=srow.subject_entity_key,
        predicate=srow.predicate,
        source_object=srow.object_value,
        target_object=trow.object_value,
        winner=winner,
        reason=reason,
    )


def _to_branch_info(row: Any) -> BranchInfo:
    return BranchInfo(
        id=row.id,
        book_id=row.book_id,
        name=row.name,
        parent=row.parent,
        status=row.status.value,
        base_beat=row.base_beat,
        base_tx=row.base_tx,
        note=row.note,
    )


__all__ = ["BranchError", "BranchService"]
