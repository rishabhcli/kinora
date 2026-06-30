"""The deterministic policy / rule engine — findings + policy → an action.

Pure functions, exhaustively testable. The engine resolves a bag of
:class:`~app.safety.contracts.Finding` against a category-policy map into the
strictest :class:`~app.safety.taxonomy.SafetyAction`, returning *which findings
drove it* so every decision is explainable.

The engine is intentionally separate from the gateway: the gateway orchestrates
classify → rules → soften → route, but the *policy decision itself* lives here as
data + pure logic, so a tenant/provider can override the policy map and the
gateway code never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.safety.contracts import Finding
from app.safety.taxonomy import (
    DEFAULT_POLICY,
    CategoryPolicy,
    SafetyAction,
    SafetyCategory,
    Severity,
    default_policy,
)


@dataclass(frozen=True)
class RuleDecision:
    """The rule engine's verdict over a set of findings.

    Args:
        action: the strictest action across all findings.
        severity: the worst severity that drove a non-ALLOW action.
        driving: exactly the findings that produced ``action`` (worst-first).
        per_category: the resolved action for each positive category (so the
            softener knows precisely which categories to try to rewrite).
    """

    action: SafetyAction
    severity: Severity
    driving: list[Finding] = field(default_factory=list)
    per_category: dict[SafetyCategory, SafetyAction] = field(default_factory=dict)

    @property
    def categories(self) -> list[SafetyCategory]:
        seen: dict[SafetyCategory, None] = {}
        for f in sorted(self.driving, key=lambda x: x.severity, reverse=True):
            seen.setdefault(f.category, None)
        return list(seen)


@dataclass(frozen=True)
class PolicyTable:
    """A resolved policy: the per-category rules the engine evaluates against.

    ``builtin()`` returns the conservative baseline; ``with_overrides`` layers a
    partial map on top **without** ever relaxing a zero-tolerance category (the
    floor is re-asserted after the merge), so a permissive tenant/provider profile
    can never open CSAM/extremism.
    """

    rules: dict[SafetyCategory, CategoryPolicy]
    version: str = "default"

    @classmethod
    def builtin(cls) -> PolicyTable:
        return cls(rules=dict(DEFAULT_POLICY), version="default")

    def rule_for(self, category: SafetyCategory) -> CategoryPolicy:
        return self.rules.get(category, default_policy(category))

    def with_overrides(
        self,
        overrides: dict[SafetyCategory, CategoryPolicy],
        *,
        version: str,
    ) -> PolicyTable:
        merged = dict(self.rules)
        merged.update(overrides)
        # Re-assert the zero-tolerance floor: any category that is zero-tolerance
        # in the baseline stays zero-tolerance no matter what an override says.
        for cat, base in DEFAULT_POLICY.items():
            if base.zero_tolerance:
                merged[cat] = base
        return PolicyTable(rules=merged, version=version)


def evaluate(
    findings: list[Finding],
    *,
    policy: PolicyTable | None = None,
    allow_transform: bool = True,
) -> RuleDecision:
    """Resolve ``findings`` into a single :class:`RuleDecision` (pure).

    Args:
        findings: the classifier/rule findings (SAFE-only ⇒ ALLOW).
        policy: the policy table to evaluate against (baseline when ``None``).
        allow_transform: when False (the output gate, where there is no prompt to
            rewrite), a category that would ``TRANSFORM`` is escalated to
            ``QUARANTINE`` instead.
    """
    table = policy or PolicyTable.builtin()
    per_category: dict[SafetyCategory, SafetyAction] = {}
    actions: list[SafetyAction] = []
    # Pick the worst severity per category first so a category resolves once.
    worst_by_cat: dict[SafetyCategory, Finding] = {}
    for f in findings:
        if not f.positive:
            continue
        cur = worst_by_cat.get(f.category)
        if cur is None or f.severity > cur.severity:
            worst_by_cat[f.category] = f

    for cat, f in worst_by_cat.items():
        rule = table.rule_for(cat)
        act = rule.action_for(f.severity, allow_transform=allow_transform)
        per_category[cat] = act
        actions.append(act)

    action = SafetyAction.strictest(actions)
    # Driving findings = those whose category resolved to the winning action (or
    # stricter, defensive) — i.e. the reasons the gateway can cite.
    driving = [
        f
        for cat, f in worst_by_cat.items()
        if per_category[cat].rank >= action.rank and action is not SafetyAction.ALLOW
    ]
    severity = max((f.severity for f in driving), default=Severity.NONE)
    return RuleDecision(
        action=action,
        severity=severity,
        driving=sorted(driving, key=lambda x: x.severity, reverse=True),
        per_category=per_category,
    )


def softenable_categories(decision: RuleDecision) -> list[SafetyCategory]:
    """Categories in ``decision`` the softener is allowed to attempt to rewrite."""
    return [cat for cat in decision.per_category if default_policy(cat).softenable]


def unsoftenable_blocking_categories(decision: RuleDecision) -> list[SafetyCategory]:
    """Blocking/quarantining categories the softener may **not** rewrite.

    These are the ones the gateway must escalate rather than transform — a slur or
    CSAM term cannot be "tastefully framed".
    """
    return [
        cat
        for cat, act in decision.per_category.items()
        if act in (SafetyAction.BLOCK, SafetyAction.QUARANTINE)
        and not default_policy(cat).softenable
    ]


__all__ = [
    "PolicyTable",
    "RuleDecision",
    "evaluate",
    "softenable_categories",
    "unsoftenable_blocking_categories",
]
