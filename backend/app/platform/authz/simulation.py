"""Policy simulation — "what-if" before a change rolls out.

The most dangerous moment in any authorization system is the *change*: edit a
role, add a deny rule, restructure a namespace, and you can silently lock people
out or open a hole. The plane closes that gap with **simulation** — replay a set
of authorization questions against a *candidate* plane (the proposed change) and
diff its decisions against the *current* plane, so a reviewer sees exactly which
decisions flip before any of it is live.

* :func:`diff_planes` runs a scenario corpus through two planes and returns the
  decisions that changed (``newly_allowed`` / ``newly_denied``), with the reason
  on each side — the blast radius of the change;
* :func:`would_change` is the single-question form ("if I add this deny rule,
  does *this* request flip?");
* :class:`Scenario` + :func:`scenario_grid` build a corpus by taking the
  cartesian product of subjects × actions × resources, so a reviewer can sweep an
  entire surface rather than hand-pick cases.

This is the partial-evaluation feature's product face: the same machinery that
folds away known attributes powers a safe rollout, and it is all pure (no
infrastructure), so a simulation runs in CI on every policy PR.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import product
from typing import Any

from app.platform.authz.model import Effect, Resource, Subject
from app.platform.authz.sdk import AuthorizationPlane


@dataclass(frozen=True)
class Scenario:
    """One what-if question (a request document without the plane)."""

    subject: Subject | str
    action: str
    resource: Resource | tuple[str, str]
    context: Mapping[str, Any] | None = None

    @property
    def label(self) -> str:
        subj = self.subject.ref if isinstance(self.subject, Subject) else f"user:{self.subject}"
        res = self.resource.ref if isinstance(self.resource, Resource) else ":".join(self.resource)
        return f"{subj} {self.action} {res}"


@dataclass(frozen=True)
class FlippedDecision:
    """A scenario whose verdict differs between the current and candidate plane."""

    scenario: Scenario
    before: Effect
    after: Effect
    before_reason: str
    after_reason: str

    @property
    def newly_allowed(self) -> bool:
        return self.before is not Effect.ALLOW and self.after is Effect.ALLOW

    @property
    def newly_denied(self) -> bool:
        return self.before is Effect.ALLOW and self.after is not Effect.ALLOW

    def render(self) -> str:
        return f"{self.scenario.label}: {self.before.value} → {self.after.value}"


@dataclass
class SimulationResult:
    """The full diff of a candidate plane against the current plane."""

    flipped: list[FlippedDecision] = field(default_factory=list)
    total: int = 0

    @property
    def newly_allowed(self) -> list[FlippedDecision]:
        return [f for f in self.flipped if f.newly_allowed]

    @property
    def newly_denied(self) -> list[FlippedDecision]:
        return [f for f in self.flipped if f.newly_denied]

    @property
    def unchanged(self) -> int:
        return self.total - len(self.flipped)

    def render(self) -> str:
        lines = [
            f"simulation: {len(self.flipped)}/{self.total} decisions change",
            f"  newly allowed: {len(self.newly_allowed)}",
            f"  newly denied:  {len(self.newly_denied)}",
        ]
        lines.extend(f"  {f.render()}" for f in self.flipped)
        return "\n".join(lines)


def diff_planes(
    current: AuthorizationPlane,
    candidate: AuthorizationPlane,
    scenarios: Iterable[Scenario],
) -> SimulationResult:
    """Run ``scenarios`` through both planes and collect the decisions that flip.

    Both planes are evaluated synchronously (pure engines), so a simulation needs
    no infrastructure and is safe to run in CI on a policy change.
    """
    result = SimulationResult()
    for scenario in scenarios:
        result.total += 1
        before = current.check_sync(
            scenario.subject, scenario.action, scenario.resource, scenario.context
        )
        after = candidate.check_sync(
            scenario.subject, scenario.action, scenario.resource, scenario.context
        )
        if before.effect is not after.effect:
            result.flipped.append(
                FlippedDecision(
                    scenario=scenario,
                    before=before.effect,
                    after=after.effect,
                    before_reason=before.explanation.replace("\n", "; "),
                    after_reason=after.explanation.replace("\n", "; "),
                )
            )
    return result


def would_change(
    current: AuthorizationPlane,
    candidate: AuthorizationPlane,
    scenario: Scenario,
) -> FlippedDecision | None:
    """Single-question what-if: the flip for ``scenario`` (or ``None`` if same)."""
    result = diff_planes(current, candidate, [scenario])
    return result.flipped[0] if result.flipped else None


def scenario_grid(
    subjects: Sequence[Subject | str],
    actions: Sequence[str],
    resources: Sequence[Resource | tuple[str, str]],
    *,
    context: Mapping[str, Any] | None = None,
) -> list[Scenario]:
    """Cartesian product of subjects × actions × resources → a scenario corpus.

    Lets a reviewer sweep an entire surface ("every role × every verb × every
    resource") rather than hand-write each case.
    """
    return [
        Scenario(subject=s, action=a, resource=r, context=context)
        for s, a, r in product(subjects, actions, resources)
    ]


__all__ = [
    "FlippedDecision",
    "Scenario",
    "SimulationResult",
    "diff_planes",
    "scenario_grid",
    "would_change",
]
