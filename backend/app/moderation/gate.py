"""The two product safety gates: ingest screening + generation safety (§9/§10).

A *gate* is the policy-enforcement boundary the rest of the pipeline calls. It
composes the pure pieces (classifier → policy engine) with the durable side
effects (event log, review queue, escalation, audit) into a single decision an
ingest/render caller can act on.

Two gates, one mechanism:

* **Ingest gate** (:meth:`SafetyGate.screen_book_text` / ``screen_page``) — runs
  at import on the *source* book, **before** the canon is built, so a wholesale-
  disallowed book is rejected before tokens/credits are spent. Fails **closed**
  on a degraded classifier by default (a source we couldn't screen is held).
* **Generation gate** (:meth:`SafetyGate.screen_clip` / ``screen_keyframe``) —
  runs on every generated keyframe/clip **before** it reaches the reader. This
  is **complementary to the §9.5 Critic**: the Critic enforces canon fidelity,
  this enforces policy. Fails **open** on a degraded classifier by default (the
  Critic + later passes still run; a transient blip never silently drops a clip)
  — but the tenant policy can flip this to fail-closed.

The gate's :class:`GateResult` carries the coarse :class:`Decision`
(``PASS`` / ``HOLD`` / ``REJECT``) plus the full verdict, the persisted event id,
and any review-item id, so the caller can branch and the UI can explain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.moderation.audit import AuditAction, ModerationAuditLog
from app.moderation.classifier import ContentClassifier
from app.moderation.contracts import (
    ClassificationResult,
    Decision,
    ModerationContext,
    ModerationVerdict,
    Surface,
)
from app.moderation.escalation import EscalationService
from app.moderation.policy import evaluate, merge_verdicts
from app.moderation.repositories import ModerationEventRepo
from app.moderation.review import ReviewWorkflow
from app.moderation.taxonomy import Disposition, ModerationCategory, Severity
from app.moderation.tenant_policy import DEFAULT_TENANT_POLICY, TenantPolicy

if TYPE_CHECKING:
    pass

logger = get_logger("app.moderation.gate")


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of one gate call — what the caller acts on."""

    decision: Decision
    verdict: ModerationVerdict
    event_id: str | None = None
    review_item_id: str | None = None
    #: When the actor is currently generation-suspended/banned (generation gate).
    actor_blocked: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.PASS

    @property
    def rejected(self) -> bool:
        return self.decision is Decision.REJECT

    @property
    def held(self) -> bool:
        return self.decision is Decision.HOLD


class GateDeps:
    """The durable collaborators a :class:`SafetyGate` writes through.

    Bundled so the gate has one constructor arg and tests can pass fakes/spies.
    """

    def __init__(
        self,
        *,
        events: ModerationEventRepo,
        review: ReviewWorkflow,
        audit: ModerationAuditLog,
        escalation: EscalationService | None = None,
    ) -> None:
        self.events = events
        self.review = review
        self.audit = audit
        self.escalation = escalation


class SafetyGate:
    """Enforce moderation policy at the ingest + generation boundaries.

    The gate is constructed per-request with a session-bound :class:`GateDeps` and
    the resolved tenant :class:`TenantPolicy`; the classifier is the injected seam
    (fake in tests). All side effects are written through ``deps`` so the audit
    log is the complete record of the decision.
    """

    def __init__(
        self,
        classifier: ContentClassifier,
        deps: GateDeps,
        *,
        policy: TenantPolicy = DEFAULT_TENANT_POLICY,
    ) -> None:
        self._classifier = classifier
        self._deps = deps
        self._policy = policy

    # -- ingest gate (source book) ------------------------------------------- #

    async def screen_book_text(
        self, text: str, *, context: ModerationContext
    ) -> GateResult:
        """Screen source book text at import (fails closed on degraded by default)."""
        result = await self._classifier.classify_text(text, surface=Surface.INGEST_TEXT)
        return await self._decide(result, context=context, fail_closed=True)

    async def screen_page(
        self, frames: list[bytes], *, context: ModerationContext
    ) -> GateResult:
        """Screen a rendered source page image at import (fails closed on degraded)."""
        result = await self._classifier.classify_frames(frames, surface=Surface.INGEST_PAGE)
        return await self._decide(result, context=context, fail_closed=True)

    # -- generation gate (produced media) ------------------------------------ #

    async def screen_keyframe(
        self, frame: bytes, *, context: ModerationContext
    ) -> GateResult:
        """Screen a generated keyframe before it is used/shown (fails open on degraded)."""
        result = await self._classifier.classify_frames([frame], surface=Surface.KEYFRAME)
        return await self._decide(result, context=context, fail_closed=False)

    async def screen_clip(
        self,
        frames: list[bytes],
        *,
        context: ModerationContext,
        narration: str | None = None,
    ) -> GateResult:
        """Screen a generated clip (frames + optional narration) before the reader.

        Complementary to the §9.5 Critic: a clip can pass canon QA and still be
        blocked here, and vice-versa. When narration text is supplied it is screened
        too and the two verdicts merged (strictest wins).
        """
        frame_result = await self._classifier.classify_frames(frames, surface=Surface.CLIP)
        verdicts = [evaluate(frame_result, policy=self._policy)]
        results = [frame_result]
        if narration:
            text_result = await self._classifier.classify_text(
                narration, surface=Surface.NARRATION
            )
            verdicts.append(evaluate(text_result, policy=self._policy))
            results.append(text_result)
        verdict = merge_verdicts(verdicts)
        degraded = any(r.degraded for r in results)
        return await self._finalize(
            verdict, context=context, fail_closed=False, degraded=degraded
        )

    async def screen_comment(
        self, text: str, *, context: ModerationContext
    ) -> GateResult:
        """Screen a free-text director comment before it reaches the crew."""
        result = await self._classifier.classify_text(text, surface=Surface.COMMENT)
        return await self._decide(result, context=context, fail_closed=False)

    # -- core decision path -------------------------------------------------- #

    async def _decide(
        self,
        result: ClassificationResult,
        *,
        context: ModerationContext,
        fail_closed: bool,
    ) -> GateResult:
        verdict = evaluate(result, policy=self._policy)
        return await self._finalize(
            verdict, context=context, fail_closed=fail_closed, degraded=result.degraded
        )

    async def _finalize(
        self,
        verdict: ModerationVerdict,
        *,
        context: ModerationContext,
        fail_closed: bool,
        degraded: bool,
    ) -> GateResult:
        """Apply degraded posture, persist the event, queue review, escalate, audit."""
        verdict, force_hold = self._apply_degraded_posture(
            verdict, fail_closed=fail_closed, degraded=degraded
        )

        event = await self._deps.events.record(
            verdict,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            book_id=context.book_id,
            shot_id=context.shot_id,
            session_id=context.session_id,
            correlation_id=context.correlation_id,
        )
        await self._deps.audit.record(
            tenant_id=context.tenant_id,
            action=AuditAction.SCREEN,
            actor_id=context.user_id or "system",
            target_id=event.id,
            payload={
                "surface": verdict.surface.value,
                "decision": verdict.decision.value,
                "severity": int(verdict.severity),
                "categories": [c.value for c in verdict.categories],
                "degraded": degraded,
            },
        )

        decision, review_item_id = await self._enforce(
            verdict, context=context, event_id=event.id, force_hold=force_hold
        )
        actor_blocked = await self._maybe_escalate(verdict, context=context)

        logger.info(
            "moderation.gate.decision",
            surface=verdict.surface.value,
            decision=decision.value,
            disposition=verdict.decision.value,
            severity=int(verdict.severity),
            tenant=context.tenant_id,
            book_id=context.book_id,
            shot_id=context.shot_id,
            degraded=degraded,
        )
        return GateResult(
            decision=decision,
            verdict=verdict,
            event_id=event.id,
            review_item_id=review_item_id,
            actor_blocked=actor_blocked,
        )

    def _apply_degraded_posture(
        self, verdict: ModerationVerdict, *, fail_closed: bool, degraded: bool
    ) -> tuple[ModerationVerdict, bool]:
        """When a classifier degraded, honour the fail-open/closed posture.

        Returns ``(verdict, force_hold)``. The tenant policy's
        ``fail_closed_on_degraded`` overrides the per-surface default toward
        strictness (it can make a fail-open surface fail closed, never the
        reverse). When fail-closed fires, the content is **always held** (never
        served) regardless of ``serve_flagged`` — a thing we could not screen is
        not served — which is what ``force_hold`` signals to :meth:`_enforce`.
        """
        if not degraded:
            return verdict, False
        closed = fail_closed or self._policy.fail_closed_on_degraded
        if not closed:
            return verdict, False  # fail open: the (likely ALLOW/empty) verdict stands
        # Fail closed: hold the content for review at MEDIUM severity.
        held = verdict.model_copy(
            update={
                "decision": Disposition.FLAG
                if verdict.decision is Disposition.ALLOW
                else verdict.decision,
                "severity": max(verdict.severity, Severity.MEDIUM),
                "reason": (verdict.reason + " · degraded-fail-closed").strip(" ·"),
            }
        )
        return held, True

    async def _enforce(
        self,
        verdict: ModerationVerdict,
        *,
        context: ModerationContext,
        event_id: str,
        force_hold: bool = False,
    ) -> tuple[Decision, str | None]:
        """Map the verdict's disposition to a coarse decision + queue review."""
        if verdict.decision is Disposition.ALLOW:
            return Decision.PASS, None

        if verdict.decision is Disposition.BLOCK:
            item = await self._deps.review.enqueue(
                verdict,
                tenant_id=context.tenant_id,
                event_id=event_id,
                user_id=context.user_id,
                book_id=context.book_id,
                shot_id=context.shot_id,
                session_id=context.session_id,
                takedown=False,
            )
            return Decision.REJECT, item.id

        # FLAG: auto-takedown when severe enough; serve-or-hold per tenant policy.
        takedown = verdict.severity >= self._policy.auto_takedown_at
        item = await self._deps.review.enqueue(
            verdict,
            tenant_id=context.tenant_id,
            event_id=event_id,
            user_id=context.user_id,
            book_id=context.book_id,
            shot_id=context.shot_id,
            session_id=context.session_id,
            takedown=takedown,
        )
        if takedown:
            return Decision.REJECT, item.id
        # A served flag PASSes (surfaced for review but shown); otherwise it's HELD.
        # A degraded-fail-closed flag is always held (never served), no matter the
        # tenant's serve_flagged setting.
        serve = self._policy.serve_flagged and not force_hold
        decision = Decision.PASS if serve else Decision.HOLD
        return decision, item.id

    async def _maybe_escalate(
        self, verdict: ModerationVerdict, *, context: ModerationContext
    ) -> bool:
        """Count a block/auto-takedown against the actor and report if blocked."""
        if self._deps.escalation is None or context.user_id is None:
            return False
        # Only hard outcomes (BLOCK, or a FLAG severe enough to auto-takedown) count.
        counts = verdict.decision is Disposition.BLOCK or (
            verdict.decision is Disposition.FLAG
            and verdict.severity >= self._policy.auto_takedown_at
        )
        if not counts:
            # Still report the actor's standing so a suspended user is gated even
            # on an allowed/soft-flagged action.
            outcome = await self._deps.escalation.status(
                tenant_id=context.tenant_id, actor_id=context.user_id
            )
            return outcome.generation_blocked
        outcome = await self._deps.escalation.record_violation(
            tenant_id=context.tenant_id,
            actor_id=context.user_id,
            severity=verdict.severity,
            categories=verdict.categories or [ModerationCategory.OTHER],
            source="gate",
            now=datetime.now(UTC),
        )
        return outcome.generation_blocked


__all__ = ["GateDeps", "GateResult", "SafetyGate"]
