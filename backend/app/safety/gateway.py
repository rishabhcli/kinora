"""The :class:`SafetyGateway` façade — the single entry point the pipeline calls.

One object the render pipeline and the video router talk to:

* :meth:`screen_prompt` — pre-generation: returns a :class:`PromptDecision` (with
  the softened prompt + routing plan) and records it to the immutable decision log.
* :meth:`screen_output` — post-generation: returns an :class:`OutputAssessment`
  (allow / quarantine / block) over sampled frames and records it.
* :meth:`tag_book` — the age-rating / content-advisory pass over a book's text.
* :meth:`override` / :meth:`request_appeal` / :meth:`resolve_appeal` — the
  appeal/override hooks against a recorded decision.
* :meth:`verify_log` — replay + tamper-check a tenant's decision chain.

The gateway composes the injected seams (classifier, softener, gates, profile
registry, decision log). :func:`build_default_gateway` wires the offline/test
defaults (deterministic fakes + in-memory log) with no providers; the composition
root passes wired providers + a DB-backed log in production.

When :attr:`SafetySettings.enabled` is False the gateway short-circuits to an
ALLOW decision (still recorded), so it can ship dark.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.safety.advisory import rate_findings
from app.safety.classifier import (
    KeywordSafetyClassifier,
    SafetyClassifier,
    build_default_classifier,
)
from app.safety.config import SafetySettings, get_safety_settings
from app.safety.contracts import (
    AppealState,
    ContentAdvisory,
    DecisionRecordView,
    OutputAssessment,
    OutputVerdict,
    PromptDecision,
    RoutingPlan,
    SafetyAction,
    SafetyContext,
    SafetySurface,
    Severity,
    SofteningResult,
)
from app.safety.decision_log import DecisionLog, DecisionRecord, InMemoryDecisionLog
from app.safety.output_gate import OutputGate
from app.safety.profiles import ProfileRegistry
from app.safety.prompt_gate import PromptGate
from app.safety.routing import plan_routing
from app.safety.rules import PolicyTable, RuleDecision
from app.safety.softener import PromptSoftener, RuleSoftener, build_default_softener

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.providers import Providers

logger = get_logger("app.safety.gateway")


class SafetyGateway:
    """The content-safety gateway for the generation pipeline."""

    def __init__(
        self,
        *,
        classifier: SafetyClassifier,
        softener: PromptSoftener | None = None,
        decision_log: DecisionLog | None = None,
        registry: ProfileRegistry | None = None,
        policy: PolicyTable | None = None,
        settings: SafetySettings | None = None,
    ) -> None:
        self._settings = settings or get_safety_settings()
        self._classifier = classifier
        self._registry = registry or ProfileRegistry.builtin()
        self._policy = policy or PolicyTable.builtin()
        self._log: DecisionLog = decision_log or InMemoryDecisionLog()
        soft: PromptSoftener
        if not self._settings.enable_softening:
            soft = _NoopSoftener()
        else:
            soft = softener or RuleSoftener()
        self._prompt_gate = PromptGate(
            classifier=classifier,
            softener=soft,
            policy=self._policy,
            registry=self._registry,
        )
        self._output_gate = OutputGate(
            classifier=classifier,
            policy=self._policy,
            fail_closed=self._settings.output_fail_closed,
        )

    @property
    def decision_log(self) -> DecisionLog:
        return self._log

    @property
    def profiles(self) -> ProfileRegistry:
        return self._registry

    # -- pre-generation --------------------------------------------------- #

    async def screen_prompt(
        self,
        prompt: str,
        *,
        context: SafetyContext | None = None,
        surface: SafetySurface = SafetySurface.PROMPT,
        candidates: list[str] | None = None,
        record: bool = True,
    ) -> PromptDecision:
        """Screen a render prompt; soften if needed; plan routing; log the decision."""
        ctx = context or SafetyContext()
        if not self._settings.enabled:
            decision = _allow_passthrough(prompt, surface, self._registry, candidates)
        else:
            decision = await self._prompt_gate.screen(
                prompt, surface=surface, candidates=candidates
            )
        if record:
            await self._log.record_prompt(decision, context=ctx)
        logger.info(
            "safety.prompt.screened",
            action=decision.action.value,
            categories=[c.value for c in decision.categories],
            transformed=decision.transformed,
            providers=decision.routing.ordered_providers if decision.routing else [],
            shot_id=ctx.shot_id,
        )
        return decision

    # -- post-generation -------------------------------------------------- #

    async def screen_output(
        self,
        frames: list[bytes],
        *,
        context: SafetyContext | None = None,
        surface: SafetySurface = SafetySurface.CLIP,
        record: bool = True,
    ) -> OutputAssessment:
        """Screen a generated keyframe / clip's sampled frames; log the verdict."""
        ctx = context or SafetyContext()
        if not self._settings.enabled:
            assessment = OutputAssessment(
                surface=surface,
                verdict=OutputVerdict.ALLOW,
                severity=Severity.NONE,
                driving_findings=[],
                sampled_frames=len(frames),
                classifier="disabled",
                reason="safety gateway disabled — pass-through",
            )
        else:
            assessment = await self._output_gate.screen_frames(frames, surface=surface)
        if record:
            await self._log.record_output(assessment, context=ctx)
        logger.info(
            "safety.output.screened",
            verdict=assessment.verdict.value,
            categories=[c.value for c in assessment.categories],
            frames=assessment.sampled_frames,
            shot_id=ctx.shot_id,
        )
        return assessment

    # -- book advisory ---------------------------------------------------- #

    async def tag_book(
        self,
        text: str,
        *,
        context: SafetyContext | None = None,
        record: bool = True,
    ) -> ContentAdvisory:
        """Classify a book's text and produce its age-rating / advisory card."""
        ctx = context or SafetyContext()
        assessment = await self._classifier.classify_text(text, surface=SafetySurface.BOOK)
        advisory = rate_findings(assessment.findings)
        if record:
            await self._record_advisory(advisory, ctx)
        return advisory

    async def _record_advisory(
        self, advisory: ContentAdvisory, ctx: SafetyContext
    ) -> DecisionRecord:
        from app.safety.contracts import DecisionKind

        log = self._log
        # Only the in-memory log exposes the low-level append; a DB log records via
        # its own advisory hook. Use the public record_output-like path defensively.
        if isinstance(log, InMemoryDecisionLog):
            return log._append(  # noqa: SLF001 - same-package controlled access
                tenant_id=ctx.tenant_id,
                kind=DecisionKind.ADVISORY,
                action=advisory.rating.value,
                severity=Severity.NONE,
                categories=tuple(advisory.category_severity),
                reason=advisory.rationale,
                surface=str(SafetySurface.BOOK),
                context=ctx,
                references=None,
                appeal_state=AppealState.NONE,
                payload={
                    "rating": advisory.rating.value,
                    "descriptors": advisory.descriptors,
                    "rationale": advisory.rationale,
                },
            )
        raise NotImplementedError  # pragma: no cover - DB log supplies its own hook

    # -- routing-only helper ---------------------------------------------- #

    async def plan_for_prompt(
        self, prompt: str, *, candidates: list[str] | None = None
    ) -> RoutingPlan:
        """Routing plan for a prompt **without** softening — what providers refuse it."""
        assessment = await self._classifier.classify_text(
            prompt, surface=SafetySurface.PROMPT
        )
        return plan_routing(
            assessment.findings, registry=self._registry, candidates=candidates
        )

    # -- appeal / override hooks ------------------------------------------ #

    async def override(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        new_action: str,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord:
        return await self._log.record_override(
            record_id=record_id,
            context=context,
            new_action=new_action,
            actor_id=actor_id,
            reason=reason,
        )

    async def request_appeal(
        self, *, record_id: str, context: SafetyContext, reason: str
    ) -> DecisionRecord:
        return await self._log.request_appeal(
            record_id=record_id, context=context, reason=reason
        )

    async def resolve_appeal(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        granted: bool,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord:
        return await self._log.resolve_appeal(
            record_id=record_id,
            context=context,
            granted=granted,
            actor_id=actor_id,
            reason=reason,
        )

    async def history(self, tenant_id: str) -> list[DecisionRecordView]:
        records = await self._log.history(tenant_id)
        return [r.to_view() for r in records]

    async def verify_log(self, tenant_id: str) -> bool:
        return (await self._log.verify(tenant_id)).intact


class _NoopSoftener:
    """A softener that never rewrites — used when softening is disabled in config."""

    name = "noop"

    async def soften(self, prompt: str, *, decision: RuleDecision) -> SofteningResult:
        return SofteningResult(
            changed=False, original_prompt=prompt, softened_prompt=prompt
        )


def _allow_passthrough(
    prompt: str,
    surface: SafetySurface,
    registry: ProfileRegistry,
    candidates: list[str] | None,
) -> PromptDecision:
    """The disabled-gateway pass-through decision (still routed, still logged)."""
    routing = plan_routing([], registry=registry, candidates=candidates)
    return PromptDecision(
        surface=surface,
        action=SafetyAction.ALLOW,
        severity=Severity.NONE,
        driving_findings=[],
        effective_prompt=prompt,
        routing=routing,
        classifier="disabled",
        reason="safety gateway disabled — pass-through",
    )


def build_default_gateway(
    providers: Providers | None = None,
    *,
    settings: Settings | None = None,
    safety_settings: SafetySettings | None = None,
    decision_log: DecisionLog | None = None,
    registry: ProfileRegistry | None = None,
    policy: PolicyTable | None = None,
) -> SafetyGateway:
    """Wire a :class:`SafetyGateway` — offline/test defaults, or the model lane.

    With no providers (tests, offline) the deterministic keyword classifier + rule
    softener + in-memory hash-chained log are used: **zero network, zero spend**.
    The composition root passes wired providers and (optionally) a DB-backed log;
    the model lanes are only used when the corresponding ``SAFETY_USE_MODEL_*`` flag
    is set AND providers are available.
    """
    sset = safety_settings or get_safety_settings()
    classifier: SafetyClassifier
    softener: PromptSoftener
    if providers is not None and settings is not None and sset.use_model_classifier:
        classifier = build_default_classifier(providers, settings=settings)
    else:
        classifier = KeywordSafetyClassifier()
    if providers is not None and settings is not None and sset.use_model_softener:
        softener = build_default_softener(providers, settings=settings)
    else:
        softener = RuleSoftener()
    return SafetyGateway(
        classifier=classifier,
        softener=softener,
        decision_log=decision_log,
        registry=registry,
        policy=policy,
        settings=sset,
    )


__all__ = ["SafetyGateway", "build_default_gateway"]
