"""Temporal compaction / GC for the bitemporal store (kinora.md §8.5, §8.7).

A bitemporal store grows monotonically: every correction leaves the superseded belief behind
(closed in transaction-time) so "canon as of any past write" stays answerable. Over a
multi-year adaptation that history is unbounded. Compaction is the bounded-storage policy:
**collapse transaction-time history older than a retention horizon while preserving the
audit hash-chain** — the audit log remains the tamper-evident record of *what* changed, so
we can prune the redundant superseded *state* rows without losing provenance.

Two safe operations, both opt-in and idempotent:

* :meth:`plan` — a dry run: report which superseded ``bitemporal_states`` rows are eligible
  (``tx_to`` closed *and* older than the horizon), grouped by fact, never touching anything.
* :meth:`compact` — delete the eligible superseded rows. The **current** belief of every
  fact is always kept (it is what forward reads need), and at least the most recent
  superseded belief inside the horizon is kept so a near-past "as-of" still resolves. The
  audit chain is never touched, so verification still passes after compaction.

This never deletes valid-time history (a *retired* fact keeps its row — that is §8.5
forgetting, not garbage); it only prunes transaction-time *redundancy* beyond the horizon.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.db.models.bitemporal import BitemporalState
from app.db.repositories.bitemporal import BitemporalStateRepo
from app.memory.bitemporal import utcnow


class CompactionPlan(BaseModel):
    """A dry-run report of what compaction would prune (no mutation)."""

    book_id: str
    branch: str
    horizon_days: int
    eligible_row_ids: list[str] = Field(default_factory=list)
    #: fact_key -> number of superseded beliefs that would be pruned.
    by_fact: dict[str, int] = Field(default_factory=dict)
    kept_current: int = 0

    @property
    def prune_count(self) -> int:
        return len(self.eligible_row_ids)


class CompactionResult(BaseModel):
    """The outcome of a compaction run."""

    book_id: str
    branch: str
    pruned: int = 0
    facts_touched: int = 0


class TemporalCompactor:
    """Prune redundant superseded tx-rows beyond a retention horizon (audit-safe)."""

    def __init__(self, states: BitemporalStateRepo, *, now: datetime | None = None) -> None:
        self._states = states
        self._now = now or utcnow()

    async def plan(
        self, *, book_id: str, branch: str, horizon_days: int = 30
    ) -> CompactionPlan:
        """Identify prunable superseded rows without mutating anything."""
        cutoff = self._now - timedelta(days=horizon_days)
        rows = await self._all_rows(book_id, branch)
        eligible, by_fact, kept_current = self._eligible(rows, cutoff)
        return CompactionPlan(
            book_id=book_id,
            branch=branch,
            horizon_days=horizon_days,
            eligible_row_ids=[r.id for r in eligible],
            by_fact=dict(by_fact),
            kept_current=kept_current,
        )

    async def compact(
        self, *, book_id: str, branch: str, horizon_days: int = 30
    ) -> CompactionResult:
        """Delete prunable superseded rows (the current belief is always retained)."""
        plan = await self.plan(book_id=book_id, branch=branch, horizon_days=horizon_days)
        if plan.eligible_row_ids:
            await self._states.session.execute(
                delete(BitemporalState).where(
                    BitemporalState.id.in_(plan.eligible_row_ids)
                )
            )
            await self._states.session.flush()
        return CompactionResult(
            book_id=book_id,
            branch=branch,
            pruned=plan.prune_count,
            facts_touched=len(plan.by_fact),
        )

    # --- internals ---------------------------------------------------------- #

    async def _all_rows(self, book_id: str, branch: str) -> list[BitemporalState]:
        stmt = (
            select(BitemporalState)
            .where(
                BitemporalState.book_id == book_id,
                BitemporalState.branch == branch,
            )
            .order_by(BitemporalState.fact_key, BitemporalState.tx_from)
        )
        return list((await self._states.session.execute(stmt)).scalars().all())

    @staticmethod
    def _eligible(
        rows: list[BitemporalState], cutoff: datetime
    ) -> tuple[list[BitemporalState], dict[str, int], int]:
        """Pick prunable rows: superseded (``tx_to`` set), closed before ``cutoff``.

        Per fact we keep the current belief (``tx_to IS NULL``) and the single most-recent
        superseded belief (so a near-past as-of still resolves); everything older than that
        and beyond the horizon is eligible.
        """
        by_key: dict[str, list[BitemporalState]] = defaultdict(list)
        for row in rows:
            by_key[row.fact_key].append(row)

        eligible: list[BitemporalState] = []
        by_fact: dict[str, int] = {}
        kept_current = 0
        for _key, group in by_key.items():
            current = [r for r in group if r.tx_to is None]
            kept_current += len(current)
            superseded = sorted(
                (r for r in group if r.tx_to is not None),
                key=lambda r: r.tx_to or r.tx_from,
            )
            # Keep the newest superseded belief; consider the rest for pruning.
            prunable_candidates = superseded[:-1] if superseded else []
            pruned_here = [
                r for r in prunable_candidates if (r.tx_to is not None and r.tx_to < cutoff)
            ]
            if pruned_here:
                eligible.extend(pruned_here)
                by_fact[_key] = len(pruned_here)
        return eligible, by_fact, kept_current


__all__ = ["CompactionPlan", "CompactionResult", "TemporalCompactor"]
