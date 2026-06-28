"""The moderation service façade (§9/§10).

One object the API and pipeline call. It owns the wiring: given a session-bound
set of repositories + the injected classifier seam, it resolves the tenant policy
(persisted override → builtin preset → conservative default), constructs the
:class:`~app.moderation.gate.SafetyGate`, and exposes the high-level operations:

* screening (ingest text/page, generated keyframe/clip, comment),
* the review-queue workflow (claim/approve/reject/takedown/appeal/...),
* the escalation status/reinstate operations,
* the tamper-evident audit replay,
* per-tenant policy read/write.

The service is constructed per unit-of-work from a session; a thin
:class:`ModerationFactory` builds one from a session + the process-wide classifier
so the composition root can hand routes a single dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.moderation.audit import AuditChainView, ModerationAuditLog
from app.moderation.classifier import ContentClassifier, KeywordClassifier
from app.moderation.contracts import ModerationContext, ReviewItemView, ReviewState
from app.moderation.escalation import (
    DEFAULT_ESCALATION_POLICY,
    EscalationOutcome,
    EscalationPolicy,
    EscalationService,
)
from app.moderation.gate import GateDeps, GateResult, SafetyGate
from app.moderation.repositories import (
    ModerationAuditRepo,
    ModerationEventRepo,
    ReviewItemRepo,
    TenantPolicyRepo,
    ViolationCounterRepo,
)
from app.moderation.review import ReviewWorkflow
from app.moderation.tenant_policy import (
    TenantPolicy,
    builtin_policies,
)

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.providers import Providers

logger = get_logger("app.moderation.service")


class ModerationService:
    """Session-bound façade over the moderation subsystem."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        classifier: ContentClassifier,
        escalation_policy: EscalationPolicy = DEFAULT_ESCALATION_POLICY,
    ) -> None:
        self._session = session
        self._classifier = classifier
        self._events = ModerationEventRepo(session)
        self._review_repo = ReviewItemRepo(session)
        self._policy_repo = TenantPolicyRepo(session)
        self._counter_repo = ViolationCounterRepo(session)
        self._audit = ModerationAuditLog(ModerationAuditRepo(session))
        self._escalation = EscalationService(
            self._counter_repo, self._audit, policy=escalation_policy
        )
        self._workflow = ReviewWorkflow(
            self._review_repo, self._audit, escalation=self._escalation
        )

    # -- policy resolution --------------------------------------------------- #

    async def resolve_policy(self, tenant_id: str) -> TenantPolicy:
        """The effective policy: persisted override → builtin preset → default."""
        row = await self._policy_repo.get(tenant_id)
        if row is not None:
            try:
                return TenantPolicy.model_validate(row.policy)
            except Exception as exc:  # noqa: BLE001 - never let a bad blob break a gate
                logger.warning(
                    "moderation.policy.invalid_blob", tenant_id=tenant_id, error=str(exc)
                )
        presets = builtin_policies()
        if tenant_id in presets:
            return presets[tenant_id]
        return presets["default"].model_copy(update={"tenant_id": tenant_id})

    async def set_policy(self, policy: TenantPolicy) -> TenantPolicy:
        """Persist a tenant policy + write an audit record of the change."""
        await self._policy_repo.upsert(
            tenant_id=policy.tenant_id,
            version=policy.version,
            strictness=policy.strictness,
            fail_closed_on_degraded=policy.fail_closed_on_degraded,
            serve_flagged=policy.serve_flagged,
            policy=policy.model_dump(mode="json"),
        )
        from app.moderation.audit import AuditAction

        await self._audit.record(
            tenant_id=policy.tenant_id,
            action=AuditAction.POLICY_CHANGE,
            actor_id="system",
            target_id=policy.tenant_id,
            payload={"version": policy.version, "strictness": policy.strictness},
        )
        return policy

    async def _gate(self, tenant_id: str) -> SafetyGate:
        policy = await self.resolve_policy(tenant_id)
        deps = GateDeps(
            events=self._events,
            review=self._workflow,
            audit=self._audit,
            escalation=self._escalation,
        )
        return SafetyGate(self._classifier, deps, policy=policy)

    # -- screening (the two product gates) ----------------------------------- #

    async def screen_book_text(self, text: str, *, context: ModerationContext) -> GateResult:
        gate = await self._gate(context.tenant_id)
        return await gate.screen_book_text(text, context=context)

    async def screen_page(self, frames: list[bytes], *, context: ModerationContext) -> GateResult:
        gate = await self._gate(context.tenant_id)
        return await gate.screen_page(frames, context=context)

    async def screen_keyframe(self, frame: bytes, *, context: ModerationContext) -> GateResult:
        gate = await self._gate(context.tenant_id)
        return await gate.screen_keyframe(frame, context=context)

    async def screen_clip(
        self,
        frames: list[bytes],
        *,
        context: ModerationContext,
        narration: str | None = None,
    ) -> GateResult:
        gate = await self._gate(context.tenant_id)
        return await gate.screen_clip(frames, context=context, narration=narration)

    async def screen_comment(self, text: str, *, context: ModerationContext) -> GateResult:
        gate = await self._gate(context.tenant_id)
        return await gate.screen_comment(text, context=context)

    # -- review workflow ----------------------------------------------------- #

    @property
    def review(self) -> ReviewWorkflow:
        return self._workflow

    async def queue(
        self, tenant_id: str, *, state: ReviewState | None = None, limit: int = 100
    ) -> list[ReviewItemView]:
        return await self._workflow.queue(tenant_id, state=state, limit=limit)

    async def queue_counts(self, tenant_id: str) -> dict[str, int]:
        return await self._review_repo.count_by_state(tenant_id)

    # -- escalation ---------------------------------------------------------- #

    @property
    def escalation(self) -> EscalationService:
        return self._escalation

    async def actor_status(self, *, tenant_id: str, actor_id: str) -> EscalationOutcome:
        return await self._escalation.status(tenant_id=tenant_id, actor_id=actor_id)

    # -- audit --------------------------------------------------------------- #

    async def audit_chain(self, tenant_id: str, *, limit: int | None = None) -> AuditChainView:
        return await self._audit.replay(tenant_id, limit=limit)

    async def event_stats(self, tenant_id: str) -> dict[str, Any]:
        """A compact moderation dashboard payload for a tenant."""
        decisions = await self._events.decision_counts(tenant_id)
        queue = await self._review_repo.count_by_state(tenant_id)
        offenders = await self._escalation.offenders(tenant_id)
        return {
            "decisions": decisions,
            "queue": queue,
            "offenders": [
                {"actor_id": o.actor_id, "tier": o.tier.label, "total": o.total_count}
                for o in offenders
            ],
        }


class ModerationFactory:
    """Builds a session-bound :class:`ModerationService` (the DI seam).

    Holds the process-wide classifier (the injectable seam) + escalation policy,
    so the composition root constructs one factory and routes call ``build(session)``
    per unit-of-work. With no providers it defaults to the keyword classifier, so a
    health check / test never forces a network dependency.
    """

    def __init__(
        self,
        *,
        classifier: ContentClassifier | None = None,
        providers: Providers | None = None,
        settings: Settings | None = None,
        escalation_policy: EscalationPolicy = DEFAULT_ESCALATION_POLICY,
    ) -> None:
        if classifier is not None:
            self._classifier: ContentClassifier = classifier
        else:
            from app.moderation.classifier import build_default_classifier

            self._classifier = build_default_classifier(providers, settings=settings)
        self._escalation_policy = escalation_policy

    @property
    def classifier(self) -> ContentClassifier:
        return self._classifier

    def build(self, session: AsyncSession) -> ModerationService:
        return ModerationService(
            session,
            classifier=self._classifier,
            escalation_policy=self._escalation_policy,
        )


def keyword_factory() -> ModerationFactory:
    """A factory backed by the deterministic keyword classifier (offline/tests)."""
    return ModerationFactory(classifier=KeywordClassifier())


__all__ = ["ModerationFactory", "ModerationService", "keyword_factory"]
