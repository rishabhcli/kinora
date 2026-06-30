"""The pre-generation PROMPT gate: classify → rules → soften → route → decide.

The gate is the gateway's pre-generation brain. For one render prompt it:

1. **Classifies** the text (rule fake / model seam) into findings.
2. Runs the **rule engine** to get the strictest action + the driving findings.
3. On a softenable, non-blocking action it **auto-softens** the prompt
   (intent-preserving), re-classifies the softened text, and re-evaluates — so a
   literary-violence prompt that would be rejected becomes a TRANSFORM that
   proceeds with a tasteful rewrite.
4. Computes a **routing plan** over the per-provider policy profiles so the caller
   can avoid a provider that would still reject the (softened) content.
5. Emits a typed, explainable :class:`~app.safety.contracts.PromptDecision`.

Pure orchestration over the injected seams; the only async is the classifier /
softener (both deterministic fakes in tests). Fail-open on a degraded classifier
*for non-floor categories*: a provider blip never silently blocks a render, but a
keyword-detected zero-tolerance hit still blocks even when the model lane is down
(the prefilter is merged in by the classifier).
"""

from __future__ import annotations

from app.safety.classifier import SafetyClassifier
from app.safety.contracts import (
    PromptAssessment,
    PromptDecision,
    SafetyAction,
    SafetySurface,
)
from app.safety.profiles import ProfileRegistry
from app.safety.routing import plan_routing
from app.safety.rules import (
    PolicyTable,
    RuleDecision,
    evaluate,
    softenable_categories,
)
from app.safety.softener import PromptSoftener, RuleSoftener


class PromptGate:
    """Pre-generation prompt screening with intent-preserving auto-softening."""

    def __init__(
        self,
        *,
        classifier: SafetyClassifier,
        softener: PromptSoftener | None = None,
        policy: PolicyTable | None = None,
        registry: ProfileRegistry | None = None,
    ) -> None:
        self._classifier = classifier
        self._softener = softener or RuleSoftener()
        self._policy = policy or PolicyTable.builtin()
        self._registry = registry or ProfileRegistry.builtin()

    async def screen(
        self,
        prompt: str,
        *,
        surface: SafetySurface = SafetySurface.PROMPT,
        candidates: list[str] | None = None,
    ) -> PromptDecision:
        assessment = await self._classifier.classify_text(prompt, surface=surface)
        decision = evaluate(
            assessment.findings, policy=self._policy, allow_transform=True
        )

        softening = None
        effective_prompt = prompt
        findings_for_routing = assessment.findings

        # Try intent-preserving softening BEFORE accepting any non-ALLOW action, as
        # long as the action is driven (at least in part) by a softenable category.
        # This is the product's core promise: literary violence/sexuality/gore is
        # rewritten into tasteful framing rather than hard-blocked. A purely
        # non-softenable action (hate, CSAM) skips softening and stands.
        if (
            decision.action is not SafetyAction.ALLOW
            and softenable_categories(decision)
        ):
            pre = decision
            softening = await self._softener.soften(prompt, decision=pre)
            if softening.changed and softening.intent_preserved:
                effective_prompt = softening.softened_prompt
                resoftened = await self._classifier.classify_text(
                    effective_prompt, surface=surface
                )
                findings_for_routing = resoftened.findings
                post = evaluate(
                    resoftened.findings, policy=self._policy, allow_transform=True
                )
                # Final action is the post-soften action; if softening fully cleared
                # the softenable content we still flag TRANSFORM (a rewrite happened),
                # and any non-softenable residue (hate) keeps its stricter action.
                decision = _reconcile_transform(pre, post)

        routing = plan_routing(
            findings_for_routing, registry=self._registry, candidates=candidates
        )

        # If the chosen action lets us proceed but NO provider can take the content,
        # downgrade to QUARANTINE: there is nothing to render against.
        action = decision.action
        proceeding = action in (SafetyAction.ALLOW, SafetyAction.TRANSFORM)
        if proceeding and not routing.has_viable_provider:
            action = SafetyAction.QUARANTINE

        reason = _reason(action, decision, softening, routing)
        return PromptDecision(
            surface=surface,
            action=action,
            severity=decision.severity,
            driving_findings=decision.driving,
            effective_prompt=effective_prompt,
            softening=softening,
            routing=routing,
            policy_version=self._policy.version,
            classifier=assessment.classifier,
            degraded=assessment.degraded,
            reason=reason,
        )


def _reconcile_transform(pre: RuleDecision, post: RuleDecision) -> RuleDecision:
    """Combine the pre-soften and post-soften rule decisions.

    The *post*-soften action governs the prompt the pipeline will actually send:

    * post ALLOW  ⇒ softening fully cleared the content. We surface ``TRANSFORM``
      (a rewrite happened) carrying the pre-soften driving findings so the UI can
      still say *what* was softened.
    * post TRANSFORM/QUARANTINE/BLOCK ⇒ a softenable category survived the rewrite
      or a non-softenable category was always present; take the post action (it is
      the honest residual risk) but cite the pre-soften driving findings, which
      describe the original offending content.
    """
    if post.action is SafetyAction.ALLOW:
        return RuleDecision(
            action=SafetyAction.TRANSFORM,
            severity=pre.severity,
            driving=pre.driving,
            per_category=pre.per_category,
        )
    return RuleDecision(
        action=post.action,
        severity=max(pre.severity, post.severity),
        driving=post.driving or pre.driving,
        per_category=post.per_category,
    )


def _reason(
    action: SafetyAction,
    decision: RuleDecision,
    softening: object,
    routing: object,
) -> str:
    cats = ", ".join(c.value for c in decision.categories) or "none"
    if action is SafetyAction.ALLOW:
        return "clean prompt — allowed"
    if action is SafetyAction.TRANSFORM:
        return f"softened literary content ({cats}) to satisfy provider policy"
    if action is SafetyAction.QUARANTINE:
        return f"held for review ({cats})"
    return f"blocked ({cats})"


def assess_only(
    assessment: PromptAssessment, *, policy: PolicyTable | None = None
) -> RuleDecision:
    """Convenience: run just the rule engine over a precomputed assessment."""
    return evaluate(assessment.findings, policy=policy or PolicyTable.builtin())


__all__ = ["PromptGate", "assess_only"]
