"""Rollout strategies: blue-green and canary (kinora.md §12.6 deployment).

A *strategy* turns "promote artifact X to environment E" into an ordered list of
:class:`RolloutStep` s. Each step shifts a fraction of traffic to the new
version, then the orchestrator verifies SLOs at that weight before advancing.
The strategy is **pure planning** — it produces the plan; the orchestrator
executes it through the traffic-router / health / SLO seams.

* **Blue-green** — bring the green (new) slot up fully at 0% traffic, verify it
  is healthy, then flip 100% of traffic in one atomic switch. Rollback = flip
  back to blue. Two steps: stage-green, cut-over.
* **Canary** — shift traffic in increasing weights (e.g. 5%, 25%, 50%, 100%),
  verifying SLOs at each weight; any breach rolls back to 0%. This is the safe
  default for the render fleet because a bad image only touches a slice of
  sessions before the SLO gate catches it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from deploy.orchestrator.models import RolloutStrategy


@dataclass(frozen=True, slots=True)
class RolloutStep:
    """One traffic-weight increment in a rollout plan.

    ``weight`` is the fraction (0.0–1.0) of traffic the *new* version should
    receive after this step. ``verify`` says whether the orchestrator must run
    SLO verification at this weight (the green-staging step verifies *health*
    only, not live SLOs, since it gets 0% traffic).
    """

    weight: float
    label: str
    verify: bool = True
    #: For blue-green: the step that brings green up at 0% before the cut-over.
    is_stage: bool = False
    #: For blue-green: the atomic 0→100 cut-over.
    is_cutover: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight must be in [0,1], got {self.weight}")


@dataclass(frozen=True, slots=True)
class RolloutPlan:
    """An ordered, validated list of :class:`RolloutStep` s."""

    strategy: RolloutStrategy
    steps: tuple[RolloutStep, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("RolloutPlan must have at least one step")
        # The final step must reach full traffic; otherwise "success" would
        # leave the old version serving some traffic forever.
        if self.steps[-1].weight != 1.0:
            raise ValueError("the final rollout step must reach weight 1.0")

    @property
    def final_weight(self) -> float:
        return self.steps[-1].weight

    def __len__(self) -> int:
        return len(self.steps)


@runtime_checkable
class Strategy(Protocol):
    """A pure rollout planner."""

    def plan(self) -> RolloutPlan:
        """Return the ordered rollout plan."""
        ...


@dataclass(frozen=True, slots=True)
class BlueGreenStrategy:
    """Blue-green: stage green at 0%, verify health, then atomic cut-over.

    Rollback is the cheapest of all strategies (flip the router back), at the
    cost of doubling the fleet during the cut-over window.
    """

    def plan(self) -> RolloutPlan:
        return RolloutPlan(
            strategy=RolloutStrategy.BLUE_GREEN,
            steps=(
                # Green up at 0% traffic; we gate on *health* (instances ready),
                # not on live SLOs, because green is not serving yet.
                RolloutStep(weight=0.0, label="stage-green", verify=False, is_stage=True),
                # Atomic flip to 100%; verify SLOs after the cut-over.
                RolloutStep(weight=1.0, label="cut-over", verify=True, is_cutover=True),
            ),
        )


@dataclass(frozen=True, slots=True)
class CanaryStep:
    """A user-facing canary step description (weight + a label)."""

    weight: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class CanaryStrategy:
    """Canary: ramp traffic through increasing weights, verifying at each.

    ``weights`` must be strictly increasing and end at 1.0. The default ladder
    (5% → 25% → 50% → 100%) limits blast radius: a bad render image only affects
    that slice of sessions before the SLO gate (§12.5) catches the breach and
    rolls back.
    """

    weights: tuple[float, ...] = (0.05, 0.25, 0.5, 1.0)
    labels: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError("canary needs at least one weight")
        if self.weights[-1] != 1.0:
            raise ValueError("canary weights must end at 1.0")
        prev = -1.0
        for w in self.weights:
            if not 0.0 < w <= 1.0:
                raise ValueError(f"canary weight {w} must be in (0,1]")
            if w <= prev:
                raise ValueError("canary weights must be strictly increasing")
            prev = w
        if self.labels and len(self.labels) != len(self.weights):
            raise ValueError("labels, when given, must match weights length")

    @classmethod
    def from_steps(cls, steps: Sequence[CanaryStep]) -> CanaryStrategy:
        return cls(
            weights=tuple(s.weight for s in steps),
            labels=tuple(s.label for s in steps),
        )

    def plan(self) -> RolloutPlan:
        steps = []
        for i, w in enumerate(self.weights):
            label = self.labels[i] if self.labels else f"canary-{int(round(w * 100))}pct"
            steps.append(RolloutStep(weight=w, label=label, verify=True))
        return RolloutPlan(strategy=RolloutStrategy.CANARY, steps=tuple(steps))


@dataclass(frozen=True, slots=True)
class RecreateStrategy:
    """Recreate: tear the old version down and bring the new one up at 100%.

    The simplest and most disruptive — a brief outage during the swap. Useful
    for the stateless ingest/mcp roles in dev. One verified full-traffic step.
    """

    def plan(self) -> RolloutPlan:
        return RolloutPlan(
            strategy=RolloutStrategy.RECREATE,
            steps=(RolloutStep(weight=1.0, label="recreate", verify=True),),
        )


def strategy_for(name: RolloutStrategy) -> Strategy:
    """Factory: return the default strategy instance for a strategy enum."""
    if name is RolloutStrategy.BLUE_GREEN:
        return BlueGreenStrategy()
    if name is RolloutStrategy.CANARY:
        return CanaryStrategy()
    if name is RolloutStrategy.RECREATE:
        return RecreateStrategy()
    raise ValueError(f"unknown strategy {name!r}")
