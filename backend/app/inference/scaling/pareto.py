"""Cost↔latency Pareto-frontier optimisation (kinora.md §11, §12.2).

Sizing one fleet to one SLO answers "how many workers". The harder operator
question is the *trade*: spend more (faster H20s, a deeper warm pool, fewer spot
reclaims) to buy lower latency, or accept higher latency to save money. There is
no single right answer — there is a **Pareto frontier** of configurations where
you cannot make latency better without making cost worse, and vice versa. This
module computes that frontier by *simulation*: it sweeps a space of
configurations (instance type × scaling policy × warm-pool depth × …) through the
discrete-event simulator (:mod:`~app.inference.scaling.simulator`) and keeps the
non-dominated set.

A configuration ``A`` *dominates* ``B`` when ``A`` is at least as good on **both**
objectives (cost, latency) and strictly better on one. We additionally treat SLO
attainment as a hard *feasibility* gate — a config that misses the SLO target is
not on the cost/latency frontier at all (it is buying cheapness you can't ship).
The result is the set of feasible, non-dominated configurations plus convenience
selectors: the cheapest feasible, the lowest-latency feasible, and the
"knee" (the point of best marginal trade, by max distance to the cost–latency
diagonal).

Pure given the candidate specs + seeds; every simulation is deterministic, so the
frontier is reproducible. Sweeps are embarrassingly parallel but run serially here
to stay dependency-free and ordering-stable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.inference.scaling.autoscaler import ScalingPolicy
from app.inference.scaling.contracts import BackendDescriptor, BackendKind
from app.inference.scaling.instances import InstanceType, default_catalog
from app.inference.scaling.simulator import (
    FleetSimulator,
    SimulationConfig,
    SimulationResult,
)
from app.inference.scaling.workload import LoadProfile

__all__ = [
    "FleetCandidate",
    "ParetoPoint",
    "ParetoFrontier",
    "ParetoSweep",
    "dominates",
]


@dataclass(frozen=True, slots=True)
class FleetCandidate:
    """One configuration to evaluate: an instance type + a scaling policy.

    ``label`` names the candidate in the report (e.g. ``"h20×warm2"``). The
    candidate is paired with the *shared* workload/SLO of the sweep at evaluation
    time, so only the fleet's own knobs vary across candidates.
    """

    label: str
    instance: InstanceType
    policy: ScalingPolicy
    concurrency: int = 1
    service_time_s: float = 5.0

    def descriptor(self, *, kind: BackendKind = BackendKind.VIDEO) -> BackendDescriptor:
        """The backend descriptor for this candidate (id derived from the label)."""
        return BackendDescriptor(
            backend_id=f"sweep::{self.label}",
            kind=kind,
            instance_type=self.instance.name,
            concurrency=self.concurrency,
            service_time_s=self.service_time_s,
        )


@dataclass(frozen=True, slots=True)
class ParetoPoint:
    """One evaluated configuration: its objectives + the underlying sim result.

    The two minimised objectives are ``cost`` (total provisioned cost) and
    ``latency_p95_ms``. ``slo_attainment`` is the feasibility metric. ``feasible``
    is whether the config cleared the sweep's SLO-attainment floor.
    """

    label: str
    cost: float
    latency_p95_ms: float
    slo_attainment: float
    feasible: bool
    result: SimulationResult = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        """JSON projection (the heavy sim result is summarised, not embedded)."""
        return {
            "label": self.label,
            "cost": round(self.cost, 6),
            "latency_p95_ms": round(self.latency_p95_ms, 3),
            "slo_attainment": round(self.slo_attainment, 4),
            "feasible": self.feasible,
            "cost_per_completed": round(self.result.cost_per_completed, 6)
            if self.result.completed
            else None,
            "peak_warm_workers": self.result.peak_warm_workers,
            "shed_rate": round(self.result.shed_rate, 4),
        }


def dominates(a: ParetoPoint, b: ParetoPoint) -> bool:
    """True when ``a`` Pareto-dominates ``b`` on (cost↓, latency↓).

    ``a`` dominates ``b`` iff it is no worse on both objectives and strictly
    better on at least one. Only meaningful among feasible points (the frontier
    filters infeasible ones out first).
    """
    no_worse = a.cost <= b.cost and a.latency_p95_ms <= b.latency_p95_ms
    strictly_better = a.cost < b.cost or a.latency_p95_ms < b.latency_p95_ms
    return no_worse and strictly_better


@dataclass(frozen=True, slots=True)
class ParetoFrontier:
    """The non-dominated, feasible configurations + convenience selectors."""

    points: tuple[ParetoPoint, ...]  # all evaluated points (feasible + not)
    frontier: tuple[ParetoPoint, ...]  # the non-dominated feasible subset

    @property
    def feasible_points(self) -> list[ParetoPoint]:
        return [p for p in self.points if p.feasible]

    def cheapest(self) -> ParetoPoint | None:
        """The lowest-cost feasible configuration on the frontier."""
        return min(self.frontier, key=lambda p: p.cost) if self.frontier else None

    def fastest(self) -> ParetoPoint | None:
        """The lowest-latency feasible configuration on the frontier."""
        return min(self.frontier, key=lambda p: p.latency_p95_ms) if self.frontier else None

    def knee(self) -> ParetoPoint | None:
        """The best-trade "knee": max normalised distance below the cost↔latency line.

        Normalise both objectives to ``[0, 1]`` across the frontier, then pick the
        point with the greatest perpendicular distance from the line joining the
        cheapest and fastest endpoints — the elbow where buying more of one
        objective stops paying off. Falls back to the single point when the
        frontier is degenerate.
        """
        if not self.frontier:
            return None
        if len(self.frontier) <= 2:
            # No interior elbow; prefer the cheaper endpoint.
            return self.cheapest()
        costs = [p.cost for p in self.frontier]
        lats = [p.latency_p95_ms for p in self.frontier]
        c_lo, c_hi = min(costs), max(costs)
        l_lo, l_hi = min(lats), max(lats)
        c_rng = (c_hi - c_lo) or 1.0
        l_rng = (l_hi - l_lo) or 1.0

        # Endpoints of the frontier line in normalised space.
        cheapest = self.cheapest()
        fastest = self.fastest()
        assert cheapest is not None and fastest is not None
        x1 = (cheapest.cost - c_lo) / c_rng
        y1 = (cheapest.latency_p95_ms - l_lo) / l_rng
        x2 = (fastest.cost - c_lo) / c_rng
        y2 = (fastest.latency_p95_ms - l_lo) / l_rng
        dx, dy = x2 - x1, y2 - y1
        line_len = math.hypot(dx, dy) or 1.0

        best: ParetoPoint | None = None
        best_dist = -1.0
        for p in self.frontier:
            px = (p.cost - c_lo) / c_rng
            py = (p.latency_p95_ms - l_lo) / l_rng
            # Perpendicular distance from the (cheapest→fastest) line.
            dist = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / line_len
            if dist > best_dist:
                best_dist, best = dist, p
        return best

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the capacity report."""
        knee = self.knee()
        cheapest = self.cheapest()
        fastest = self.fastest()
        return {
            "evaluated": len(self.points),
            "feasible": len(self.feasible_points),
            "frontier": [p.to_dict() for p in self.frontier],
            "cheapest": cheapest.label if cheapest else None,
            "fastest": fastest.label if fastest else None,
            "knee": knee.label if knee else None,
        }


@dataclass(frozen=True, slots=True)
class ParetoSweep:
    """Evaluate a set of fleet candidates against a shared workload + SLO.

    ``min_slo_attainment`` is the feasibility floor (a config must meet the SLO on
    at least this fraction of completed requests to be eligible for the frontier).
    Each candidate runs through the simulator at the shared profile/horizon/seed.
    """

    profile: LoadProfile
    horizon_s: float
    slo_target_s: float
    min_slo_attainment: float = 0.95
    committed_fraction: float = 0.4
    seed: int = 0
    autoscale_interval_s: float = 15.0

    def _evaluate(self, candidate: FleetCandidate) -> ParetoPoint:
        cfg = SimulationConfig(
            descriptor=candidate.descriptor(),
            instance=candidate.instance,
            scaling_policy=candidate.policy,
            profile=self.profile,
            horizon_s=self.horizon_s,
            slo_target_s=self.slo_target_s,
            committed_fraction=self.committed_fraction,
            autoscale_interval_s=self.autoscale_interval_s,
            seed=self.seed,
        )
        result = FleetSimulator(cfg).run()
        return ParetoPoint(
            label=candidate.label,
            cost=result.cost.total_cost,
            latency_p95_ms=result.latency.p95_ms,
            slo_attainment=result.slo_attainment,
            feasible=result.slo_attainment >= self.min_slo_attainment,
            result=result,
        )

    def run(self, candidates: Sequence[FleetCandidate]) -> ParetoFrontier:
        """Simulate every candidate and return the cost↔latency Pareto frontier."""
        points = tuple(self._evaluate(c) for c in candidates)
        feasible = [p for p in points if p.feasible]
        frontier = tuple(
            sorted(
                (p for p in feasible if not any(dominates(q, p) for q in feasible if q is not p)),
                key=lambda p: p.cost,
            )
        )
        return ParetoFrontier(points=points, frontier=frontier)


def default_candidates(
    *,
    instances: dict[str, InstanceType] | None = None,
    warm_pool_options: Sequence[int] = (0, 1, 2),
    max_workers: int = 32,
    target_tail_s: float = 60.0,
    scale_to_zero: bool = True,
    concurrency: int = 2,
    service_time_s: float = 5.0,
) -> list[FleetCandidate]:
    """A reasonable default candidate grid: every instance × each warm-pool depth.

    The grid the capacity report sweeps by default — the cross product of the
    heterogeneous catalog and a few warm-pool depths, so the frontier shows both
    the instance-type trade *and* the warm-pool trade (cold-start latency vs idle
    cost) in one picture.
    """
    catalog = instances or default_catalog()
    out: list[FleetCandidate] = []
    for inst_name, inst in catalog.items():
        for warm in warm_pool_options:
            policy = ScalingPolicy(
                min_workers=warm,
                warm_pool=warm,
                max_workers=max_workers,
                target_tail_s=target_tail_s,
                tail_quantile=0.95,
                scale_to_zero=scale_to_zero,
                max_step=8,
            )
            out.append(
                FleetCandidate(
                    label=f"{inst_name}×warm{warm}",
                    instance=inst,
                    policy=policy,
                    concurrency=concurrency,
                    service_time_s=service_time_s,
                )
            )
    return out
