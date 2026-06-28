"""Policy-as-code: the rule model + the built-in compliance rule set.

A :class:`PolicyRule` is a named, severity-tagged predicate over a snapshot of a
subject's compliance facts (:class:`ComplianceFacts`). Rules are *pure* — they
take facts and return a :class:`RuleOutcome` (decision + human message) — so the
whole rule set is unit-testable without a database, and a deployment can extend
the set without touching the engine.

The built-in rules encode the obligations this subsystem enforces:

* required consents must be granted (else processing is denied);
* consent must be to the *current* policy version (else re-consent is needed);
* model-training requires explicit consent;
* an erasure DSAR cannot complete while a legal hold is active;
* every open DSAR must be inside its statutory deadline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.compliance.consent.service import ConsentSnapshot
from app.compliance.dsar.service import DSARView
from app.compliance.enums import (
    ConsentState,
    DSARKind,
    DSARState,
    PolicyDecision,
    ProcessingPurpose,
    RuleSeverity,
)
from app.compliance.hold.service import HoldScope


@dataclass(frozen=True)
class ComplianceFacts:
    """Everything a rule needs to judge a subject, gathered once.

    Assembled by :class:`~app.compliance.service.ComplianceService` from the
    consent / hold / DSAR services so rules never touch the database themselves.
    """

    subject_id: str
    now: datetime
    consent: ConsentSnapshot
    hold: HoldScope
    dsars: tuple[DSARView, ...] = ()
    #: Purposes the product *requires* to be granted (from the active policies).
    required_purposes: frozenset[ProcessingPurpose] = frozenset()


@dataclass(frozen=True)
class RuleOutcome:
    """The result of evaluating one rule against the facts."""

    decision: PolicyDecision
    message: str
    #: Optional obligation the caller must satisfy (for ALLOW_WITH_OBLIGATION).
    obligation: str | None = None
    #: Structured evidence for the report (e.g. which purposes failed).
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when the rule did not DENY."""
        return self.decision != PolicyDecision.DENY


#: A rule's evaluation function: facts → outcome.
RuleFn = Callable[[ComplianceFacts], RuleOutcome]


@dataclass(frozen=True)
class PolicyRule:
    """A named, severity-tagged compliance rule."""

    id: str
    title: str
    severity: RuleSeverity
    evaluate: RuleFn
    #: GDPR / regulatory reference for the report (e.g. "Art. 7(1)").
    reference: str = ""


# --------------------------------------------------------------------------- #
# Built-in rule predicates
# --------------------------------------------------------------------------- #


def _required_consents_granted(facts: ComplianceFacts) -> RuleOutcome:
    missing = [
        purpose.value
        for purpose in facts.required_purposes
        if not facts.consent.for_purpose(purpose).is_granted
    ]
    if missing:
        return RuleOutcome(
            decision=PolicyDecision.DENY,
            message=f"required consent(s) not granted: {', '.join(sorted(missing))}",
            evidence={"missing": sorted(missing)},
        )
    return RuleOutcome(decision=PolicyDecision.ALLOW, message="all required consents granted")


def _consent_not_stale(facts: ComplianceFacts) -> RuleOutcome:
    stale = [c.purpose.value for c in facts.consent.purposes if c.is_stale]
    if stale:
        return RuleOutcome(
            decision=PolicyDecision.ALLOW_WITH_OBLIGATION,
            message=f"consent is to an older policy version: {', '.join(sorted(stale))}",
            obligation="prompt the subject to re-consent to the current policy version",
            evidence={"stale": sorted(stale)},
        )
    return RuleOutcome(
        decision=PolicyDecision.ALLOW, message="all consents are to the current policy version"
    )


def _model_training_requires_consent(facts: ComplianceFacts) -> RuleOutcome:
    consent = facts.consent.for_purpose(ProcessingPurpose.MODEL_TRAINING)
    if consent.state == ConsentState.GRANTED:
        return RuleOutcome(decision=PolicyDecision.ALLOW, message="model-training consent granted")
    return RuleOutcome(
        decision=PolicyDecision.DENY,
        message="model training is not permitted without explicit consent",
        evidence={"state": consent.state.value},
    )


def _erasure_not_blocked_silently(facts: ComplianceFacts) -> RuleOutcome:
    blocked = [
        d.id
        for d in facts.dsars
        if d.kind == DSARKind.ERASURE
        and d.state not in (DSARState.COMPLETED, DSARState.REJECTED, DSARState.CANCELLED)
        and facts.hold.any_active
    ]
    if blocked:
        return RuleOutcome(
            decision=PolicyDecision.ALLOW_WITH_OBLIGATION,
            message="erasure request(s) suspended by an active legal hold",
            obligation="inform the subject of the Art. 17(3) exemption / hold and the delay",
            evidence={"requests": blocked, "hold_ids": list(facts.hold.hold_ids)},
        )
    return RuleOutcome(
        decision=PolicyDecision.ALLOW, message="no erasure request is silently blocked"
    )


def _dsars_within_deadline(facts: ComplianceFacts) -> RuleOutcome:
    overdue = [d.id for d in facts.dsars if d.overdue]
    if overdue:
        return RuleOutcome(
            decision=PolicyDecision.DENY,
            message=f"{len(overdue)} DSAR(s) past their statutory deadline",
            evidence={"overdue": overdue},
        )
    return RuleOutcome(decision=PolicyDecision.ALLOW, message="all open DSARs are within deadline")


#: The shipped rule set, in report order (most-critical first).
DEFAULT_RULES: tuple[PolicyRule, ...] = (
    PolicyRule(
        id="required_consents_granted",
        title="Required consents are granted",
        severity=RuleSeverity.CRITICAL,
        evaluate=_required_consents_granted,
        reference="Art. 6(1)(a)",
    ),
    PolicyRule(
        id="model_training_requires_consent",
        title="Model training requires explicit consent",
        severity=RuleSeverity.CRITICAL,
        evaluate=_model_training_requires_consent,
        reference="Art. 6(1)(a) / Art. 22",
    ),
    PolicyRule(
        id="dsars_within_deadline",
        title="Open DSARs are within the statutory deadline",
        severity=RuleSeverity.CRITICAL,
        evaluate=_dsars_within_deadline,
        reference="Art. 12(3)",
    ),
    PolicyRule(
        id="erasure_not_blocked_silently",
        title="Erasure requests blocked by a hold are surfaced",
        severity=RuleSeverity.WARNING,
        evaluate=_erasure_not_blocked_silently,
        reference="Art. 17(3)",
    ),
    PolicyRule(
        id="consent_not_stale",
        title="Consent is to the current policy version",
        severity=RuleSeverity.WARNING,
        evaluate=_consent_not_stale,
        reference="Art. 7(1)",
    ),
)


__all__ = [
    "DEFAULT_RULES",
    "ComplianceFacts",
    "PolicyRule",
    "RuleFn",
    "RuleOutcome",
]
