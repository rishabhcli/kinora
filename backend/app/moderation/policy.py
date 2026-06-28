"""The deterministic policy engine — labels + tenant policy → verdict (§10).

This is the *intelligence-free* core: a **pure function** that turns a
:class:`~app.moderation.contracts.ClassificationResult` (a bag of model/keyword
labels) plus a :class:`~app.moderation.tenant_policy.TenantPolicy` into one
:class:`~app.moderation.contracts.ModerationVerdict`. No I/O, no model calls — so
it is exhaustively unit-testable by feeding it labels.

Resolution rules (deterministic, in order):

1. Re-bucket each label's raw ``score`` through the tenant's strictness
   multiplier, taking the **stricter** of (the classifier's own severity, the
   re-scaled severity). Strictness can tighten but never loosen a tier.
2. For each positive label, ask the tenant's :class:`CategoryRule` for its
   disposition (ALLOW / FLAG / BLOCK), honouring the zero-tolerance floor.
3. The verdict's disposition is the **strictest** across all labels; the driving
   labels are exactly those that produced that strictest disposition (so the UI
   and audit can say *why*).
4. The verdict severity is the worst severity among the driving labels.

A ``degraded`` classification (the model errored) is passed through verbatim:
the verdict carries ``degraded=True`` and the **gate** decides the posture
(fail-open vs fail-closed). The engine never silently upgrades a degraded SAFE to
a block — that policy belongs at the gate, where the surface is known.
"""

from __future__ import annotations

from app.moderation.contracts import (
    ClassificationResult,
    ContentLabel,
    ModerationVerdict,
)
from app.moderation.taxonomy import Disposition, Severity
from app.moderation.tenant_policy import DEFAULT_TENANT_POLICY, TenantPolicy


def _effective_severity(policy: TenantPolicy, label: ContentLabel) -> Severity:
    """The stricter of the label's own tier and the tenant-rescaled tier."""
    rescaled = policy.scaled_severity(label.score)
    return max(label.severity, rescaled)


def evaluate(
    result: ClassificationResult,
    *,
    policy: TenantPolicy = DEFAULT_TENANT_POLICY,
) -> ModerationVerdict:
    """Resolve a classification into a verdict under ``policy`` (pure).

    See the module docstring for the resolution order. Returns an ALLOW verdict
    with no driving labels when nothing fires.
    """
    scored: list[tuple[ContentLabel, Severity, Disposition]] = []
    for label in result.positive_labels():
        severity = _effective_severity(policy, label)
        disposition = policy.rule_for(label.category).disposition_for(severity)
        # Re-stamp the label with its effective severity so the audit/UI shows the
        # tier the decision was actually made on (not the raw classifier tier).
        effective_label = label.model_copy(update={"severity": severity})
        scored.append((effective_label, severity, disposition))

    if not scored:
        return ModerationVerdict(
            surface=result.surface,
            decision=Disposition.ALLOW,
            severity=Severity.NONE,
            driving_labels=[],
            classifier=result.classifier,
            policy_version=policy.version,
            degraded=result.degraded,
            reason="no policy-relevant labels",
        )

    decision = Disposition.strictest([d for _, _, d in scored])
    driving = [lab for lab, _, d in scored if d is decision]
    severity = max((s for _, s, d in scored if d is decision), default=Severity.NONE)
    reason = _reason(decision, driving)
    return ModerationVerdict(
        surface=result.surface,
        decision=decision,
        severity=severity,
        driving_labels=driving,
        classifier=result.classifier,
        policy_version=policy.version,
        degraded=result.degraded,
        reason=reason,
    )


def _reason(decision: Disposition, driving: list[ContentLabel]) -> str:
    """A one-line human explanation of the verdict (for the feed + audit)."""
    if not driving:
        return "clean"
    cats = ", ".join(sorted({lab.category.value for lab in driving}))
    verb = {
        Disposition.ALLOW: "allowed",
        Disposition.FLAG: "flagged",
        Disposition.BLOCK: "blocked",
    }[decision]
    worst = max(lab.severity for lab in driving)
    return f"{verb} on {cats} (severity {worst.name.lower()})"


def merge_verdicts(verdicts: list[ModerationVerdict]) -> ModerationVerdict:
    """Fold several verdicts (e.g. text + frames of the same shot) into one.

    Takes the strictest disposition; unions the driving labels of every verdict at
    that strictest tier. Used when a single piece of content is screened on more
    than one modality (a clip's frames + its narration text).
    """
    if not verdicts:
        raise ValueError("merge_verdicts requires at least one verdict")
    if len(verdicts) == 1:
        return verdicts[0]
    decision = Disposition.strictest([v.decision for v in verdicts])
    driving: list[ContentLabel] = []
    for v in verdicts:
        if v.decision is decision:
            driving.extend(v.driving_labels)
    severity = max((lab.severity for lab in driving), default=Severity.NONE)
    degraded = any(v.degraded for v in verdicts)
    return ModerationVerdict(
        surface=verdicts[0].surface,
        decision=decision,
        severity=severity,
        driving_labels=driving,
        classifier="+".join(sorted({v.classifier for v in verdicts})),
        policy_version=verdicts[0].policy_version,
        degraded=degraded,
        reason=_reason(decision, driving),
    )


__all__ = ["evaluate", "merge_verdicts"]
