"""The policy-as-code evaluation engine.

Runs a rule set against a subject's :class:`~app.compliance.policy.rules.ComplianceFacts`
and aggregates the per-rule outcomes into an overall decision: any DENY makes the
whole evaluation DENY; otherwise any obligation makes it ALLOW_WITH_OBLIGATION;
otherwise ALLOW. The engine is pure (no DB) and isolates each rule so a buggy
custom rule cannot crash the evaluation — it surfaces as a failed rule instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.compliance.enums import PolicyDecision, RuleSeverity
from app.compliance.policy.rules import (
    DEFAULT_RULES,
    ComplianceFacts,
    PolicyRule,
    RuleOutcome,
)
from app.core.logging import get_logger

logger = get_logger("app.compliance.policy")


@dataclass(frozen=True)
class RuleResult:
    """One rule's outcome, paired with the rule's identity for reporting."""

    rule_id: str
    title: str
    severity: RuleSeverity
    reference: str
    outcome: RuleOutcome

    @property
    def passed(self) -> bool:
        return self.outcome.passed


class PolicyEngine:
    """Evaluate a rule set against compliance facts."""

    def __init__(self, rules: Sequence[PolicyRule] = DEFAULT_RULES) -> None:
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        """The rule set this engine evaluates (report order)."""
        return self._rules

    def evaluate(self, facts: ComplianceFacts) -> list[RuleResult]:
        """Evaluate every rule, isolating failures so one cannot abort the run."""
        results: list[RuleResult] = []
        for rule in self._rules:
            try:
                outcome = rule.evaluate(facts)
            except Exception as exc:  # noqa: BLE001 - a buggy rule must not crash audit
                logger.warning("compliance.policy.rule_error", rule_id=rule.id, error=str(exc))
                outcome = RuleOutcome(
                    decision=PolicyDecision.DENY,
                    message=f"rule evaluation failed: {exc}",
                    evidence={"error": str(exc)},
                )
            results.append(
                RuleResult(
                    rule_id=rule.id,
                    title=rule.title,
                    severity=rule.severity,
                    reference=rule.reference,
                    outcome=outcome,
                )
            )
        return results

    @staticmethod
    def overall(results: Sequence[RuleResult]) -> PolicyDecision:
        """Fold per-rule outcomes into a single decision (DENY wins, then obligation)."""
        decisions = {r.outcome.decision for r in results}
        if PolicyDecision.DENY in decisions:
            return PolicyDecision.DENY
        if PolicyDecision.ALLOW_WITH_OBLIGATION in decisions:
            return PolicyDecision.ALLOW_WITH_OBLIGATION
        return PolicyDecision.ALLOW


__all__ = ["PolicyEngine", "RuleResult"]
