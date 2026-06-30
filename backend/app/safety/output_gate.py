"""The post-generation OUTPUT gate: sampled-frame classification → verdict.

After a provider returns a keyframe / clip, the gateway screens the *generated*
pixels before the reader ever sees them. There is no prompt to rewrite here, so
the gate has only three outcomes: ``ALLOW`` (show it), ``QUARANTINE`` (hold for
review — not shown), or ``BLOCK`` (destroy — a zero-tolerance hit on output). The
rule engine is run with ``allow_transform=False`` so a would-be TRANSFORM is
escalated to QUARANTINE.

Frame sampling lives in the classifier seam (a long clip is sampled to a bounded
set of frames); this gate just maps the classifier's findings to a verdict and
records how many frames were inspected for the audit trail.

Fail posture: a *degraded* classifier (the model lane errored) on a generation
output **fails open** — the clip is allowed rather than silently dropped — because
the prompt was already screened pre-generation and a provider blip must not stall
the reading room. A deployment that wants the stricter posture flips
``fail_closed=True``.
"""

from __future__ import annotations

from app.safety.classifier import SafetyClassifier
from app.safety.contracts import (
    OutputAssessment,
    OutputVerdict,
    SafetySurface,
    Severity,
)
from app.safety.rules import PolicyTable, evaluate
from app.safety.taxonomy import SafetyAction


class OutputGate:
    """Post-generation screening of a generated keyframe / clip."""

    def __init__(
        self,
        *,
        classifier: SafetyClassifier,
        policy: PolicyTable | None = None,
        fail_closed: bool = False,
    ) -> None:
        self._classifier = classifier
        self._policy = policy or PolicyTable.builtin()
        self._fail_closed = fail_closed

    async def screen_frames(
        self,
        frames: list[bytes],
        *,
        surface: SafetySurface = SafetySurface.CLIP,
    ) -> OutputAssessment:
        assessment = await self._classifier.classify_frames(frames, surface=surface)
        decision = evaluate(
            assessment.findings, policy=self._policy, allow_transform=False
        )

        if assessment.degraded:
            verdict = (
                OutputVerdict.QUARANTINE if self._fail_closed else OutputVerdict.ALLOW
            )
            reason = (
                "classifier degraded — failing closed (held for review)"
                if self._fail_closed
                else "classifier degraded — failing open (prompt was pre-screened)"
            )
            return OutputAssessment(
                surface=surface,
                verdict=verdict,
                severity=Severity.NONE,
                driving_findings=[],
                sampled_frames=len(frames),
                classifier=assessment.classifier,
                policy_version=self._policy.version,
                degraded=True,
                reason=reason,
            )

        verdict = _action_to_verdict(decision.action)
        cats = ", ".join(c.value for c in decision.categories) or "none"
        reason = {
            OutputVerdict.ALLOW: "clean output — allowed",
            OutputVerdict.QUARANTINE: f"held for review ({cats})",
            OutputVerdict.BLOCK: f"blocked output ({cats})",
        }[verdict]
        return OutputAssessment(
            surface=surface,
            verdict=verdict,
            severity=decision.severity,
            driving_findings=decision.driving,
            sampled_frames=len(frames),
            classifier=assessment.classifier,
            policy_version=self._policy.version,
            reason=reason,
        )


def _action_to_verdict(action: SafetyAction) -> OutputVerdict:
    """Map a rule action to a post-generation verdict (TRANSFORM ⇒ QUARANTINE)."""
    if action is SafetyAction.BLOCK:
        return OutputVerdict.BLOCK
    if action in (SafetyAction.QUARANTINE, SafetyAction.TRANSFORM):
        return OutputVerdict.QUARANTINE
    return OutputVerdict.ALLOW


__all__ = ["OutputGate"]
