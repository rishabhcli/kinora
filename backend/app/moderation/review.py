"""Human-review queue + takedown/appeal workflow state machine (§10).

When a screening FLAGs content (or a tenant auto-takes-down a high-severity flag),
a :class:`~app.moderation.models.ReviewItem` is enqueued. A reviewer claims it and
either approves (content cleared) or rejects/takes it down; the owner may appeal a
rejection, which a senior reviewer grants or denies. Anything can be escalated.

The legal transitions are a small, explicit graph — :data:`TRANSITIONS` — and
:func:`can_transition` is a **pure** predicate over it, so the whole state machine
is unit-testable without a DB. :class:`ReviewWorkflow` then drives the repo +
audit log to apply a validated transition durably.

State graph::

    PENDING ──claim──▶ UNDER_REVIEW ──approve──▶ APPROVED (terminal)
       │  │                  │
       │  │                  ├──reject───▶ REJECTED ──appeal──▶ APPEALED
       │  │                  └──takedown─▶ TAKEDOWN ──appeal──▶ APPEALED
       │  └──takedown──────────────────▶ TAKEDOWN
       └──escalate──▶ ESCALATED  (and ESCALATED ──claim──▶ UNDER_REVIEW)
    APPEALED ──grant──▶ APPEAL_GRANTED (terminal, reinstated)
    APPEALED ──deny───▶ APPEAL_DENIED  (terminal, stays down)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.moderation.audit import AuditAction, ModerationAuditLog
from app.moderation.contracts import (
    TERMINAL_REVIEW_STATES,
    ModerationVerdict,
    ReviewItemView,
    ReviewState,
    Surface,
)
from app.moderation.models import ReviewItem
from app.moderation.repositories import ReviewItemRepo
from app.moderation.taxonomy import Disposition, ModerationCategory, Severity

if TYPE_CHECKING:
    from app.moderation.escalation import EscalationService

logger = get_logger("app.moderation.review")


class ReviewTransitionError(ValueError):
    """Raised when an illegal review-state transition is attempted."""


#: The explicit legal-transition graph (pure data).
TRANSITIONS: dict[ReviewState, frozenset[ReviewState]] = {
    ReviewState.PENDING: frozenset(
        {ReviewState.UNDER_REVIEW, ReviewState.TAKEDOWN, ReviewState.ESCALATED}
    ),
    ReviewState.UNDER_REVIEW: frozenset(
        {
            ReviewState.APPROVED,
            ReviewState.REJECTED,
            ReviewState.TAKEDOWN,
            ReviewState.ESCALATED,
        }
    ),
    ReviewState.ESCALATED: frozenset(
        {
            ReviewState.UNDER_REVIEW,
            ReviewState.APPROVED,
            ReviewState.REJECTED,
            ReviewState.TAKEDOWN,
        }
    ),
    ReviewState.REJECTED: frozenset({ReviewState.APPEALED, ReviewState.TAKEDOWN}),
    ReviewState.TAKEDOWN: frozenset({ReviewState.APPEALED}),
    ReviewState.APPEALED: frozenset({ReviewState.APPEAL_GRANTED, ReviewState.APPEAL_DENIED}),
    # Terminal states: no outgoing edges.
    ReviewState.APPROVED: frozenset(),
    ReviewState.APPEAL_GRANTED: frozenset(),
    ReviewState.APPEAL_DENIED: frozenset(),
}


def can_transition(current: ReviewState, target: ReviewState) -> bool:
    """Whether ``current → target`` is a legal review-state transition (pure)."""
    return target in TRANSITIONS.get(current, frozenset())


def is_terminal(state: ReviewState) -> bool:
    """Whether ``state`` is terminal (no further transitions)."""
    return state in TERMINAL_REVIEW_STATES or not TRANSITIONS.get(state, frozenset())


def content_reinstated(state: ReviewState) -> bool:
    """Whether the content should be (re)served given the final review state."""
    return state in {ReviewState.APPROVED, ReviewState.APPEAL_GRANTED}


def content_down(state: ReviewState) -> bool:
    """Whether the content must stay removed/hidden given the review state."""
    return state in {
        ReviewState.REJECTED,
        ReviewState.TAKEDOWN,
        ReviewState.APPEAL_DENIED,
    }


def _now(now: datetime | None) -> datetime:
    from datetime import UTC

    return now or datetime.now(UTC)


class ReviewWorkflow:
    """Drive the review queue + state machine durably (repo + audit log).

    Every method validates the transition with :func:`can_transition` (raising
    :class:`ReviewTransitionError` on an illegal move), applies it via the repo,
    and writes a matching audit entry — so the moderation log is the complete,
    tamper-evident record of who moved what and when.

    An optional :class:`~app.moderation.escalation.EscalationService` is notified
    when a rejection/takedown is upheld so the offending actor's tally advances.
    """

    def __init__(
        self,
        repo: ReviewItemRepo,
        audit: ModerationAuditLog,
        *,
        escalation: EscalationService | None = None,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._escalation = escalation

    async def enqueue(
        self,
        verdict: ModerationVerdict,
        *,
        tenant_id: str,
        event_id: str | None = None,
        user_id: str | None = None,
        book_id: str | None = None,
        shot_id: str | None = None,
        session_id: str | None = None,
        takedown: bool = False,
        payload: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> ReviewItem:
        """Create a review item for a flagged/taken-down verdict (+ audit)."""
        state = ReviewState.TAKEDOWN if takedown else ReviewState.PENDING
        item = await self._repo.enqueue(
            tenant_id=tenant_id,
            surface=verdict.surface,
            decision=verdict.decision,
            severity=int(verdict.severity),
            categories=[c.value for c in verdict.categories],
            reason=verdict.reason,
            state=state,
            event_id=event_id,
            user_id=user_id,
            book_id=book_id,
            shot_id=shot_id,
            session_id=session_id,
            payload=payload,
            now=_now(now),
        )
        action = AuditAction.TAKEDOWN if takedown else AuditAction.ENQUEUE_REVIEW
        await self._audit.record(
            tenant_id=tenant_id,
            action=action,
            actor_id="system",
            target_id=item.id,
            payload={
                "surface": verdict.surface.value,
                "decision": verdict.decision.value,
                "severity": int(verdict.severity),
                "categories": [c.value for c in verdict.categories],
                "reason": verdict.reason,
            },
        )
        return item

    async def _apply(
        self,
        item_id: str,
        *,
        target: ReviewState,
        action: AuditAction,
        actor_id: str,
        note: str | None,
        assignee_id: str | None = None,
        resolved: bool = False,
        now: datetime | None = None,
    ) -> ReviewItem:
        item = await self._repo.get(item_id)
        if item is None:
            raise ReviewTransitionError(f"no such review item: {item_id}")
        if not can_transition(item.state, target):
            raise ReviewTransitionError(
                f"illegal transition {item.state.value} -> {target.value}"
            )
        await self._repo.transition(
            item,
            to_state=target,
            actor_id=actor_id,
            note=note,
            assignee_id=assignee_id,
            resolved=resolved,
            now=_now(now),
        )
        await self._audit.record(
            tenant_id=item.tenant_id,
            action=action,
            actor_id=actor_id,
            target_id=item.id,
            payload={"to_state": target.value, "note": note},
        )
        return item

    async def claim(self, item_id: str, *, reviewer_id: str) -> ReviewItem:
        """A reviewer claims a pending/escalated item → UNDER_REVIEW."""
        return await self._apply(
            item_id,
            target=ReviewState.UNDER_REVIEW,
            action=AuditAction.CLAIM_REVIEW,
            actor_id=reviewer_id,
            note=None,
            assignee_id=reviewer_id,
        )

    async def approve(
        self, item_id: str, *, reviewer_id: str, note: str | None = None
    ) -> ReviewItem:
        """Clear the content — APPROVED (terminal). The gate may now serve it."""
        return await self._apply(
            item_id,
            target=ReviewState.APPROVED,
            action=AuditAction.APPROVE,
            actor_id=reviewer_id,
            note=note,
            resolved=True,
        )

    async def reject(
        self, item_id: str, *, reviewer_id: str, note: str | None = None
    ) -> ReviewItem:
        """Uphold the block — REJECTED. Advances the actor's offender tally."""
        item = await self._apply(
            item_id,
            target=ReviewState.REJECTED,
            action=AuditAction.REJECT,
            actor_id=reviewer_id,
            note=note,
            resolved=True,
        )
        await self._on_upheld(item)
        return item

    async def takedown(
        self, item_id: str, *, reviewer_id: str, note: str | None = None
    ) -> ReviewItem:
        """Pull live content — TAKEDOWN. Advances the actor's offender tally."""
        item = await self._apply(
            item_id,
            target=ReviewState.TAKEDOWN,
            action=AuditAction.TAKEDOWN,
            actor_id=reviewer_id,
            note=note,
            resolved=True,
        )
        await self._on_upheld(item)
        return item

    async def escalate(self, item_id: str, *, actor_id: str, note: str | None = None) -> ReviewItem:
        """Bump to a senior reviewer / policy owner — ESCALATED."""
        return await self._apply(
            item_id,
            target=ReviewState.ESCALATED,
            action=AuditAction.ESCALATE,
            actor_id=actor_id,
            note=note,
        )

    async def appeal(
        self, item_id: str, *, appellant_id: str, note: str | None = None
    ) -> ReviewItem:
        """The owner contests a rejection/takedown — APPEALED."""
        return await self._apply(
            item_id,
            target=ReviewState.APPEALED,
            action=AuditAction.APPEAL,
            actor_id=appellant_id,
            note=note,
        )

    async def grant_appeal(
        self, item_id: str, *, reviewer_id: str, note: str | None = None
    ) -> ReviewItem:
        """Appeal succeeds — APPEAL_GRANTED (terminal, reinstated)."""
        return await self._apply(
            item_id,
            target=ReviewState.APPEAL_GRANTED,
            action=AuditAction.APPEAL_GRANT,
            actor_id=reviewer_id,
            note=note,
            resolved=True,
        )

    async def deny_appeal(
        self, item_id: str, *, reviewer_id: str, note: str | None = None
    ) -> ReviewItem:
        """Appeal fails — APPEAL_DENIED (terminal, stays down)."""
        return await self._apply(
            item_id,
            target=ReviewState.APPEAL_DENIED,
            action=AuditAction.APPEAL_DENY,
            actor_id=reviewer_id,
            note=note,
            resolved=True,
        )

    async def _on_upheld(self, item: ReviewItem) -> None:
        """A human upheld a block/takedown → count it against the actor."""
        if self._escalation is None or item.user_id is None:
            return
        await self._escalation.record_violation(
            tenant_id=item.tenant_id,
            actor_id=item.user_id,
            severity=Severity(item.severity),
            categories=[ModerationCategory(c) for c in (item.categories or [])],
            source="review",
        )

    async def queue(
        self, tenant_id: str, *, state: ReviewState | None = None, limit: int = 100
    ) -> list[ReviewItemView]:
        rows = await self._repo.list_queue(tenant_id, state=state, limit=limit)
        return [project_item(r) for r in rows]

    async def view(self, item_id: str) -> ReviewItemView | None:
        row = await self._repo.get(item_id)
        return project_item(row) if row is not None else None


def project_item(row: ReviewItem) -> ReviewItemView:
    """Project a :class:`ReviewItem` ORM row into the API read-model."""
    return ReviewItemView(
        id=row.id,
        tenant_id=row.tenant_id,
        surface=Surface(row.surface),
        state=ReviewState(row.state),
        decision=Disposition(row.decision),
        severity=Severity(row.severity),
        categories=[ModerationCategory(c) for c in (row.categories or [])],
        book_id=row.book_id,
        shot_id=row.shot_id,
        user_id=row.user_id,
        reason=row.reason,
        assignee_id=row.assignee_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        payload=row.payload,
    )


__all__ = [
    "TRANSITIONS",
    "ReviewTransitionError",
    "ReviewWorkflow",
    "can_transition",
    "content_down",
    "content_reinstated",
    "is_terminal",
    "project_item",
]
