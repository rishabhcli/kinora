"""The consolidated compliance report.

Turns a policy-engine evaluation into a structured, serialisable report a DPO can
read or a dashboard can render: the overall decision, per-rule pass/fail with
severity and references, the obligations the operator must satisfy, and a compact
fact summary (consent state, holds, open DSARs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.compliance.enums import ConsentState, PolicyDecision, RuleSeverity
from app.compliance.policy.engine import PolicyEngine, RuleResult
from app.compliance.policy.rules import ComplianceFacts


@dataclass(frozen=True)
class ComplianceReport:
    """A consolidated, serialisable compliance report for one subject."""

    subject_id: str
    generated_at: datetime
    decision: PolicyDecision
    results: tuple[RuleResult, ...]
    obligations: tuple[str, ...]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def is_compliant(self) -> bool:
        """True when no rule denied (obligations are still 'compliant, with action')."""
        return self.decision != PolicyDecision.DENY

    @property
    def failures(self) -> tuple[RuleResult, ...]:
        """The rules that denied, most-severe first."""
        order = {RuleSeverity.CRITICAL: 0, RuleSeverity.WARNING: 1, RuleSeverity.INFO: 2}
        failed = [r for r in self.results if not r.passed]
        return tuple(sorted(failed, key=lambda r: order[r.severity]))

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable projection (for the API / ledger / export)."""
        return {
            "subject_id": self.subject_id,
            "generated_at": self.generated_at.isoformat(),
            "decision": self.decision.value,
            "is_compliant": self.is_compliant,
            "obligations": list(self.obligations),
            "summary": self.summary,
            "rules": [
                {
                    "id": r.rule_id,
                    "title": r.title,
                    "severity": r.severity.value,
                    "reference": r.reference,
                    "decision": r.outcome.decision.value,
                    "passed": r.passed,
                    "message": r.outcome.message,
                    "obligation": r.outcome.obligation,
                    "evidence": r.outcome.evidence,
                }
                for r in self.results
            ],
        }


def _fact_summary(facts: ComplianceFacts) -> dict[str, Any]:
    granted = [c.purpose.value for c in facts.consent.purposes if c.is_granted]
    withdrawn = [
        c.purpose.value for c in facts.consent.purposes if c.state == ConsentState.WITHDRAWN
    ]
    return {
        "consent_granted": sorted(granted),
        "consent_withdrawn": sorted(withdrawn),
        "legal_hold_active": facts.hold.any_active,
        "legal_hold_ids": list(facts.hold.hold_ids),
        "open_dsars": len(facts.dsars),
        "overdue_dsars": sum(1 for d in facts.dsars if d.overdue),
    }


def build_report(facts: ComplianceFacts, engine: PolicyEngine | None = None) -> ComplianceReport:
    """Evaluate the rule set against ``facts`` and assemble the report."""
    engine = engine or PolicyEngine()
    results = engine.evaluate(facts)
    decision = PolicyEngine.overall(results)
    obligations = tuple(r.outcome.obligation for r in results if r.outcome.obligation is not None)
    return ComplianceReport(
        subject_id=facts.subject_id,
        generated_at=facts.now,
        decision=decision,
        results=tuple(results),
        obligations=obligations,
        summary=_fact_summary(facts),
    )


__all__ = ["ComplianceReport", "build_report"]
