"""Repositories for the moderation subsystem (§9/§10).

Each repo wraps an :class:`AsyncSession` and owns the queries for one table. Like
every repo in this codebase they **flush, never commit** — the unit-of-work
boundary owns the transaction.

* :class:`ModerationEventRepo` — record + query screening outcomes.
* :class:`ModerationAuditRepo` — append + replay the hash-chained audit log.
* :class:`ReviewItemRepo` — the human-review queue (claim / resolve / appeal).
* :class:`TenantPolicyRepo` — persist + load per-tenant policy.
* :class:`ViolationCounterRepo` — the per-actor rolling violation tally.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.db.base import new_id
from app.db.repositories.base import BaseRepository
from app.moderation.contracts import (
    ModerationVerdict,
    ReviewState,
    Surface,
)
from app.moderation.models import (
    ModerationAuditEntry,
    ModerationEvent,
    ModerationTenantPolicy,
    ReviewItem,
    ViolationCounter,
)
from app.moderation.taxonomy import Disposition


def _canonical(payload: dict[str, Any] | None) -> str:
    """Deterministic JSON for hashing (sorted keys, no spaces)."""
    if payload is None:
        return ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class ModerationEventRepo(BaseRepository):
    """Record + query screening outcomes (one row per gate call)."""

    async def record(
        self,
        verdict: ModerationVerdict,
        *,
        tenant_id: str,
        user_id: str | None = None,
        book_id: str | None = None,
        shot_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
    ) -> ModerationEvent:
        row = ModerationEvent(
            id=event_id or new_id(),
            tenant_id=tenant_id,
            surface=verdict.surface,
            decision=verdict.decision,
            severity=int(verdict.severity),
            classifier=verdict.classifier,
            policy_version=verdict.policy_version,
            degraded=verdict.degraded,
            reason=verdict.reason,
            user_id=user_id,
            book_id=book_id,
            shot_id=shot_id,
            session_id=session_id,
            correlation_id=correlation_id,
            categories=[c.value for c in verdict.categories],
            labels=[lab.model_dump(mode="json") for lab in verdict.driving_labels],
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, event_id: str) -> ModerationEvent | None:
        return await self.session.get(ModerationEvent, event_id)

    async def list_for_book(self, book_id: str, *, limit: int = 200) -> list[ModerationEvent]:
        stmt = (
            select(ModerationEvent)
            .where(ModerationEvent.book_id == book_id)
            .order_by(ModerationEvent.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def decision_counts(self, tenant_id: str) -> dict[str, int]:
        """Per-disposition counts for a tenant (the eval-harness denominator)."""
        stmt = (
            select(ModerationEvent.decision, func.count())
            .where(ModerationEvent.tenant_id == tenant_id)
            .group_by(ModerationEvent.decision)
        )
        rows = (await self.session.execute(stmt)).all()
        out = {d.value: 0 for d in Disposition}
        for decision, count in rows:
            out[decision.value] = int(count)
        return out


class ModerationAuditRepo(BaseRepository):
    """Append + replay the append-only, hash-chained moderation audit log."""

    @staticmethod
    def compute_hash(
        prev_hash: str | None,
        *,
        seq: int,
        action: str,
        actor_id: str,
        target_id: str | None,
        payload_repr: str,
    ) -> str:
        """``H(prev_hash || seq || action || actor || target || payload)`` (sha256 hex)."""
        h = hashlib.sha256()
        h.update((prev_hash or "").encode())
        h.update(f"|{seq}|{action}|{actor_id}|{target_id or ''}|{payload_repr}".encode())
        return h.hexdigest()

    async def head(self, tenant_id: str) -> ModerationAuditEntry | None:
        stmt = (
            select(ModerationAuditEntry)
            .where(ModerationAuditEntry.tenant_id == tenant_id)
            .order_by(ModerationAuditEntry.seq.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def append(
        self,
        *,
        tenant_id: str,
        action: str,
        actor_id: str,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ModerationAuditEntry:
        head = await self.head(tenant_id)
        seq = (head.seq + 1) if head is not None else 1
        prev_hash = head.entry_hash if head is not None else None
        payload_repr = _canonical(payload)
        entry_hash = self.compute_hash(
            prev_hash,
            seq=seq,
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            payload_repr=payload_repr,
        )
        row = ModerationAuditEntry(
            id=new_id(),
            tenant_id=tenant_id,
            seq=seq,
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            payload=payload,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def replay(
        self, tenant_id: str, *, limit: int | None = None
    ) -> list[ModerationAuditEntry]:
        stmt = (
            select(ModerationAuditEntry)
            .where(ModerationAuditEntry.tenant_id == tenant_id)
            .order_by(ModerationAuditEntry.seq)
        )
        if limit is not None:
            sub = (
                select(ModerationAuditEntry.id)
                .where(ModerationAuditEntry.tenant_id == tenant_id)
                .order_by(ModerationAuditEntry.seq.desc())
                .limit(limit)
            )
            ids = list((await self.session.execute(sub)).scalars().all())
            stmt = (
                select(ModerationAuditEntry)
                .where(ModerationAuditEntry.id.in_(ids))
                .order_by(ModerationAuditEntry.seq)
            )
        return list((await self.session.execute(stmt)).scalars().all())


class ReviewItemRepo(BaseRepository):
    """The human-review queue: enqueue, claim, transition, query."""

    async def enqueue(
        self,
        *,
        tenant_id: str,
        surface: Surface,
        decision: Disposition,
        severity: int,
        categories: list[str],
        reason: str,
        state: ReviewState = ReviewState.PENDING,
        event_id: str | None = None,
        user_id: str | None = None,
        book_id: str | None = None,
        shot_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ReviewItem:
        at = (now or datetime.now()).isoformat()
        row = ReviewItem(
            id=new_id(),
            tenant_id=tenant_id,
            event_id=event_id,
            surface=surface,
            state=state,
            decision=decision,
            severity=severity,
            categories=categories,
            reason=reason,
            user_id=user_id,
            book_id=book_id,
            shot_id=shot_id,
            session_id=session_id,
            state_history=[{"state": state.value, "actor": "system", "at": at}],
            payload=payload,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, item_id: str) -> ReviewItem | None:
        return await self.session.get(ReviewItem, item_id)

    async def transition(
        self,
        item: ReviewItem,
        *,
        to_state: ReviewState,
        actor_id: str,
        note: str | None = None,
        assignee_id: str | None = None,
        resolved: bool = False,
        now: datetime | None = None,
    ) -> ReviewItem:
        """Move ``item`` to ``to_state`` and append a history record (in-session)."""
        at = (now or datetime.now())
        item.state = to_state
        if assignee_id is not None:
            item.assignee_id = assignee_id
        if resolved:
            item.resolver_id = actor_id
            item.resolved_at = at
            if note is not None:
                item.resolution_note = note
        history = list(item.state_history or [])
        history.append(
            {
                "state": to_state.value,
                "actor": actor_id,
                "note": note,
                "at": at.isoformat(),
            }
        )
        item.state_history = history
        await self.session.flush()
        return item

    async def list_queue(
        self,
        tenant_id: str,
        *,
        state: ReviewState | None = None,
        limit: int = 100,
    ) -> list[ReviewItem]:
        stmt = select(ReviewItem).where(ReviewItem.tenant_id == tenant_id)
        if state is not None:
            stmt = stmt.where(ReviewItem.state == state)
        stmt = stmt.order_by(
            ReviewItem.severity.desc(), ReviewItem.created_at.asc()
        ).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_state(self, tenant_id: str) -> dict[str, int]:
        stmt = (
            select(ReviewItem.state, func.count())
            .where(ReviewItem.tenant_id == tenant_id)
            .group_by(ReviewItem.state)
        )
        rows = (await self.session.execute(stmt)).all()
        out = {s.value: 0 for s in ReviewState}
        for state, count in rows:
            out[state.value] = int(count)
        return out


class TenantPolicyRepo(BaseRepository):
    """Persist + load the configurable per-tenant policy."""

    async def get(self, tenant_id: str) -> ModerationTenantPolicy | None:
        stmt = select(ModerationTenantPolicy).where(
            ModerationTenantPolicy.tenant_id == tenant_id
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def upsert(
        self,
        *,
        tenant_id: str,
        version: str,
        strictness: float,
        fail_closed_on_degraded: bool,
        serve_flagged: bool,
        policy: dict[str, Any],
    ) -> ModerationTenantPolicy:
        row = await self.get(tenant_id)
        if row is None:
            row = ModerationTenantPolicy(id=new_id(), tenant_id=tenant_id, policy=policy)
            self.session.add(row)
        row.version = version
        row.strictness = strictness
        row.fail_closed_on_degraded = fail_closed_on_degraded
        row.serve_flagged = serve_flagged
        row.policy = policy
        await self.session.flush()
        return row

    async def list_all(self) -> list[ModerationTenantPolicy]:
        stmt = select(ModerationTenantPolicy).order_by(ModerationTenantPolicy.tenant_id)
        return list((await self.session.execute(stmt)).scalars().all())


class ViolationCounterRepo(BaseRepository):
    """Per-actor rolling violation tally for repeat-offender escalation."""

    async def get(self, tenant_id: str, actor_id: str) -> ViolationCounter | None:
        stmt = select(ViolationCounter).where(
            ViolationCounter.tenant_id == tenant_id,
            ViolationCounter.actor_id == actor_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_or_create(
        self, tenant_id: str, actor_id: str, *, now: datetime
    ) -> ViolationCounter:
        row = await self.get(tenant_id, actor_id)
        if row is None:
            row = ViolationCounter(
                id=new_id(),
                tenant_id=tenant_id,
                actor_id=actor_id,
                total_count=0,
                window_count=0,
                window_started_at=now,
                tier=0,
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def save(self, row: ViolationCounter) -> ViolationCounter:
        await self.session.flush()
        return row

    async def list_offenders(
        self, tenant_id: str, *, min_tier: int = 1, limit: int = 100
    ) -> list[ViolationCounter]:
        stmt = (
            select(ViolationCounter)
            .where(
                ViolationCounter.tenant_id == tenant_id,
                ViolationCounter.tier >= min_tier,
            )
            .order_by(ViolationCounter.tier.desc(), ViolationCounter.total_count.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "ModerationAuditRepo",
    "ModerationEventRepo",
    "ReviewItemRepo",
    "TenantPolicyRepo",
    "ViolationCounterRepo",
]
