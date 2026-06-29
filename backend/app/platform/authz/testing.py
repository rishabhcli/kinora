"""Policy testing + coverage — assert decisions and measure rule reach.

A unified plane is only safe to evolve if you can *test* the policy the way you
test code. This module provides:

* :class:`PolicyTestCase` — a single expectation: "subject S doing action A on
  resource R (in context C) should be ALLOW/DENY", optionally asserting a
  specific reason/rule fired;
* :class:`PolicySuite` — a collection of cases run against a plane, producing a
  :class:`SuiteResult` with per-case pass/fail and the failing reason;
* **coverage** — :func:`coverage_report` instruments a run to record which
  ABAC rules and which DSL allow/deny bodies actually fired across the suite, so
  a reviewer can see *dead* policy (a rule no test exercises) before rollout.

Everything is pure and synchronous: a suite runs against an all-pure plane
(``check_sync``) so policy tests need no infrastructure and run in milliseconds,
exactly like a unit test of any other pure function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.platform.authz.abac import AbacEngine
from app.platform.authz.dsl import PolicyEngine
from app.platform.authz.model import Effect, Resource, Subject
from app.platform.authz.sdk import AuthorizationPlane


@dataclass(frozen=True)
class PolicyTestCase:
    """One policy expectation evaluated against a plane.

    ``expect`` is the effect the decision must have. When ``expect_rule`` is set,
    the case also asserts that a reason naming that rule contributed (so a test
    can pin *why*, not just the verdict).
    """

    name: str
    subject: Subject | str
    action: str
    resource: Resource | tuple[str, str]
    expect: Effect
    context: Mapping[str, Any] | None = None
    expect_rule: str | None = None

    @classmethod
    def allow(
        cls, name: str, subject: Subject | str, action: str, resource: Any, **kw: Any
    ) -> PolicyTestCase:
        return cls(
            name=name,
            subject=subject,
            action=action,
            resource=resource,
            expect=Effect.ALLOW,
            **kw,
        )

    @classmethod
    def deny(
        cls, name: str, subject: Subject | str, action: str, resource: Any, **kw: Any
    ) -> PolicyTestCase:
        return cls(
            name=name,
            subject=subject,
            action=action,
            resource=resource,
            expect=Effect.DENY,
            **kw,
        )


@dataclass(frozen=True)
class CaseResult:
    """The outcome of running one :class:`PolicyTestCase`."""

    case: PolicyTestCase
    passed: bool
    actual: Effect
    detail: str

    def render(self) -> str:
        flag = "PASS" if self.passed else "FAIL"
        return (
            f"[{flag}] {self.case.name}: expected {self.case.expect.value}, "
            f"got {self.actual.value} — {self.detail}"
        )


@dataclass
class SuiteResult:
    """The aggregate result of running a :class:`PolicySuite`."""

    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.passed]

    def render(self) -> str:
        lines = [c.render() for c in self.cases]
        ok = sum(1 for c in self.cases if c.passed)
        lines.append(f"{ok}/{self.total} cases passed")
        return "\n".join(lines)


class PolicySuite:
    """A runnable collection of policy test cases."""

    def __init__(self, cases: Sequence[PolicyTestCase] = ()) -> None:
        self._cases = list(cases)

    def add(self, case: PolicyTestCase) -> PolicySuite:
        self._cases.append(case)
        return self

    @property
    def cases(self) -> tuple[PolicyTestCase, ...]:
        return tuple(self._cases)

    def run(self, plane: AuthorizationPlane) -> SuiteResult:
        """Run every case against ``plane`` (synchronously) and aggregate."""
        result = SuiteResult()
        for case in self._cases:
            decision = plane.check_sync(
                case.subject, case.action, case.resource, case.context
            )
            ok = decision.effect is case.expect
            detail = decision.explanation.replace("\n", "; ")
            if ok and case.expect_rule is not None:
                fired = any(r.rule == case.expect_rule for r in decision.reasons)
                if not fired:
                    ok = False
                    detail = f"expected rule {case.expect_rule!r} did not fire; {detail}"
            result.cases.append(
                CaseResult(case=case, passed=ok, actual=decision.effect, detail=detail)
            )
        return result


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


@dataclass
class CoverageReport:
    """Which policy rules a suite exercised — and which it never did.

    ``fired`` is the set of rule identifiers that contributed to at least one
    decision; ``declared`` is every rule the plane *could* fire. ``uncovered``
    (the difference) is dead policy a reviewer should question before rollout.
    """

    declared: frozenset[str]
    fired: frozenset[str]

    @property
    def uncovered(self) -> frozenset[str]:
        return self.declared - self.fired

    @property
    def ratio(self) -> float:
        return len(self.fired & self.declared) / len(self.declared) if self.declared else 1.0

    def render(self) -> str:
        cov = f"{self.ratio * 100:.0f}%"
        lines = [f"policy coverage: {cov} ({len(self.fired & self.declared)}/{len(self.declared)})"]
        if self.uncovered:
            lines.append("uncovered rules: " + ", ".join(sorted(self.uncovered)))
        return "\n".join(lines)


def declared_rules(plane: AuthorizationPlane) -> frozenset[str]:
    """Every rule identifier the plane's ABAC + DSL engines could fire."""
    out: set[str] = set()
    for engine in plane.engines:
        if isinstance(engine, AbacEngine):
            out |= {r.name for r in engine.rules}
        elif isinstance(engine, PolicyEngine):
            for policy in engine.policies:
                if policy.allow_rules():
                    out.add(f"{policy.package}/allow")
                if policy.deny_rules():
                    out.add(f"{policy.package}/deny")
    return frozenset(out)


def coverage_report(plane: AuthorizationPlane, suite: PolicySuite) -> CoverageReport:
    """Run ``suite`` and report which declared rules actually fired."""
    declared = declared_rules(plane)
    fired: set[str] = set()
    for case in suite.cases:
        decision = plane.check_sync(case.subject, case.action, case.resource, case.context)
        for reason in decision.reasons:
            if reason.rule is not None:
                fired.add(reason.rule)
    return CoverageReport(declared=declared, fired=frozenset(fired))


__all__ = [
    "CaseResult",
    "CoverageReport",
    "PolicySuite",
    "PolicyTestCase",
    "SuiteResult",
    "coverage_report",
    "declared_rules",
]
