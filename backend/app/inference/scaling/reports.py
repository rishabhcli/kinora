"""Capacity-planning reports for the inference fleet (kinora.md §11, §12.2, §13).

The operator-facing output of this whole facet: a single, serialisable report that
answers "to serve *this* demand at *this* SLO, what fleet do I run, what will it
cost, and where is the cost↔latency trade?". It composes the pieces:

* **the demand** — the workload profile + the reader-population coupling (§4.1);
* **the sizing** — the queue-theory :class:`~app.inference.scaling.queueing.FleetSizing`
  for the chosen instance at the demand's peak (the analytical answer);
* **the validation** — a discrete-event :class:`~app.inference.scaling.simulator.SimulationResult`
  proving the sizing's SLO attainment + cost under the actual (bursty) load;
* **the SLO verdict** — the simulated latency run through the reliability toolkit's
  :class:`~app.reliability.slo.SLOSet`, so the pass/fail uses the same machinery as
  the rest of Kinora's reliability gating;
* **the trade** — the cost↔latency :class:`~app.inference.scaling.pareto.ParetoFrontier`
  over the instance/warm-pool grid, with the recommended (knee) configuration.

The report is a pure function of its inputs; :meth:`CapacityPlanner.plan` runs the
sims and assembles it. :meth:`CapacityReport.render_text` produces a compact
human summary for a CLI / log, and :meth:`to_dict` the JSON for a dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.inference.scaling.autoscaler import ScalingPolicy
from app.inference.scaling.contracts import BackendDescriptor, BackendKind
from app.inference.scaling.instances import InstanceType, default_catalog
from app.inference.scaling.pareto import (
    ParetoFrontier,
    ParetoSweep,
    default_candidates,
)
from app.inference.scaling.queueing import FleetSizing, size_fleet
from app.inference.scaling.simulator import (
    FleetSimulator,
    SimulationConfig,
    SimulationResult,
)
from app.inference.scaling.workload import LoadProfile
from app.reliability.latency import LatencySummary
from app.reliability.slo import SLO, SLIKind, SLOSet, SLOVerdict

__all__ = [
    "CapacityReport",
    "CapacityPlanner",
    "slo_set_for_latency_target",
    "evaluate_fleet_slo",
]


def slo_set_for_latency_target(
    *, p95_target_ms: float, p99_target_ms: float, attainment_target: float = 0.95
) -> SLOSet:
    """A minimal SLO set for the fleet sim's aggregate latency (reuses §12.5 types).

    The simulator reports a single aggregate :class:`LatencySummary`, so we build
    p95/p99 latency objectives plus an availability-style attainment objective from
    the reliability toolkit's :class:`SLO` vocabulary.
    """
    return SLOSet(
        slos=(
            SLO("fleet-p95", SLIKind.LATENCY_P95, p95_target_ms, endpoint=None),
            SLO("fleet-p99", SLIKind.LATENCY_P99, p99_target_ms, endpoint=None),
            SLO("fleet-attainment", SLIKind.AVAILABILITY, attainment_target, endpoint=None),
        )
    )


def evaluate_fleet_slo(
    slo_set: SLOSet, *, latency: LatencySummary, attainment: float
) -> SLOVerdict:
    """Evaluate a fleet-sim's aggregate latency + attainment against an SLO set.

    We measure each objective directly off the :class:`LatencySummary` + the
    attainment fraction and reuse :meth:`SLO.evaluate` / :class:`SLOVerdict` — the
    same verdict type the reliability gate produces — without routing through the
    load-runner's ``LoadReport`` (whose ``_measure`` is typed to that concrete
    report). Aggregate SLOs only (``endpoint is None``).
    """
    measured: dict[SLIKind, float] = {
        SLIKind.LATENCY_P50: latency.p50_ms,
        SLIKind.LATENCY_P90: latency.p90_ms,
        SLIKind.LATENCY_P95: latency.p95_ms,
        SLIKind.LATENCY_P99: latency.p99_ms,
        SLIKind.LATENCY_P999: latency.p999_ms,
        SLIKind.AVAILABILITY: attainment,
        SLIKind.ERROR_RATE: 1.0 - attainment,
    }
    results = tuple(slo.evaluate(measured[slo.kind]) for slo in slo_set.slos)
    return SLOVerdict(passed=all(r.met for r in results), results=results)


@dataclass(frozen=True, slots=True)
class CapacityReport:
    """The assembled capacity plan: sizing + validation + SLO verdict + trade."""

    backend_id: str
    instance_type: str
    peak_demand_rps: float
    slo_target_s: float
    sizing: FleetSizing
    simulation: SimulationResult
    slo_verdict: SLOVerdict
    frontier: ParetoFrontier
    recommended_label: str | None

    @property
    def passed(self) -> bool:
        """Whether the simulated fleet met the SLO set."""
        return self.slo_verdict.passed

    def to_dict(self) -> dict[str, object]:
        """JSON projection for a dashboard / API."""
        return {
            "backend_id": self.backend_id,
            "instance_type": self.instance_type,
            "peak_demand_rps": round(self.peak_demand_rps, 5),
            "slo_target_s": self.slo_target_s,
            "passed": self.passed,
            "recommended": self.recommended_label,
            "sizing": self.sizing.to_dict(),
            "simulation": self.simulation.to_dict(),
            "slo_verdict": self.slo_verdict.to_dict(),
            "pareto": self.frontier.to_dict(),
        }

    def render_text(self) -> str:
        """A compact human-readable capacity-plan summary."""
        sim = self.simulation
        lines = [
            f"Capacity plan — {self.backend_id} ({self.instance_type})",
            f"  peak demand     : {self.peak_demand_rps:.3f} req/s",
            f"  SLO target      : <= {self.slo_target_s:.1f}s "
            f"({'PASS' if self.passed else 'FAIL'})",
            f"  analytical size : {self.sizing.servers} servers "
            f"(p{int(self.sizing.quantile * 100)} resp {self.sizing.achieved_tail_s:.1f}s)",
            f"  simulated       : {sim.completed}/{sim.admitted} completed, "
            f"attainment {sim.slo_attainment:.2%}, shed {sim.shed_rate:.2%}",
            f"  peak warm fleet : {sim.peak_warm_workers} workers",
            f"  total cost      : {sim.cost.total_cost:.4f} "
            f"(cold-start {sim.cost.cold_start_cost:.4f}, idle {sim.cost.idle_cost:.4f})",
            f"  preemptions     : {sim.preemptions}, reclaims {sim.reclaims}, "
            f"wasted {sim.wasted_compute_s:.1f}s",
        ]
        if self.recommended_label:
            knee = self.frontier.knee()
            cheapest = self.frontier.cheapest()
            fastest = self.frontier.fastest()
            lines.append(
                f"  pareto frontier : {len(self.frontier.frontier)} configs; "
                f"recommend {self.recommended_label}"
            )
            if cheapest and fastest and knee:
                lines.append(
                    f"    cheapest={cheapest.label} (${cheapest.cost:.3f}) "
                    f"fastest={fastest.label} (p95 {fastest.latency_p95_ms:.0f}ms) "
                    f"knee={knee.label}"
                )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CapacityPlanner:
    """Assembles a :class:`CapacityReport` from a demand profile + an SLO.

    ``run_pareto`` toggles the (more expensive) instance/warm-pool frontier sweep;
    ``pareto_min_attainment`` is the feasibility floor for that sweep.
    """

    profile: LoadProfile
    horizon_s: float
    slo_target_s: float = 60.0
    instance_type: str = "gpu-a10"
    concurrency: int = 2
    service_time_s: float = 5.0
    committed_fraction: float = 0.4
    seed: int = 0
    run_pareto: bool = True
    pareto_min_attainment: float = 0.9
    slo_p95_target_ms: float | None = None
    slo_p99_target_ms: float | None = None

    def _instance(self) -> InstanceType:
        return default_catalog()[self.instance_type]

    def plan(self) -> CapacityReport:
        """Run the analytical sizing, the validation sim, and the Pareto sweep."""
        instance = self._instance()
        peak_rps = self.profile.peak_rate()
        eff_service = instance.effective_service_time_s(self.service_time_s)

        # 1. Analytical sizing at peak demand (tail-latency aware).
        sizing = size_fleet(
            arrival_rate_per_s=max(1e-9, peak_rps),
            service_time_s=eff_service,
            target_response_s=self.slo_target_s,
            quantile=0.95,
            max_servers=1024,
        )

        # 2. Validation sim with the autoscaler sized around that floor.
        desc = BackendDescriptor(
            backend_id=f"plan::{self.instance_type}",
            kind=BackendKind.VIDEO,
            instance_type=self.instance_type,
            concurrency=self.concurrency,
            service_time_s=self.service_time_s,
        )
        warm = max(1, sizing.servers // 2)
        policy = ScalingPolicy(
            min_workers=warm,
            warm_pool=warm,
            max_workers=max(warm, sizing.servers * 2 + 2),
            target_tail_s=self.slo_target_s,
            tail_quantile=0.95,
            scale_to_zero=False,
            max_step=8,
        )
        sim_cfg = SimulationConfig(
            descriptor=desc,
            instance=instance,
            scaling_policy=policy,
            profile=self.profile,
            horizon_s=self.horizon_s,
            slo_target_s=self.slo_target_s,
            committed_fraction=self.committed_fraction,
            seed=self.seed,
        )
        sim = FleetSimulator(sim_cfg).run()

        # 3. SLO verdict over the simulated latency (reliability toolkit reuse).
        p95 = self.slo_p95_target_ms or self.slo_target_s * 1000.0
        p99 = self.slo_p99_target_ms or self.slo_target_s * 1000.0 * 1.2
        slo_set = slo_set_for_latency_target(
            p95_target_ms=p95, p99_target_ms=p99, attainment_target=0.95
        )
        verdict = evaluate_fleet_slo(
            slo_set, latency=sim.latency, attainment=sim.slo_attainment
        )

        # 4. The cost↔latency Pareto sweep (optional).
        if self.run_pareto:
            sweep = ParetoSweep(
                profile=self.profile,
                horizon_s=self.horizon_s,
                slo_target_s=self.slo_target_s,
                min_slo_attainment=self.pareto_min_attainment,
                committed_fraction=self.committed_fraction,
                seed=self.seed,
            )
            candidates = default_candidates(
                target_tail_s=self.slo_target_s,
                concurrency=self.concurrency,
                service_time_s=self.service_time_s,
            )
            frontier = sweep.run(candidates)
        else:
            frontier = ParetoFrontier(points=(), frontier=())

        knee = frontier.knee()
        return CapacityReport(
            backend_id=desc.backend_id,
            instance_type=self.instance_type,
            peak_demand_rps=peak_rps,
            slo_target_s=self.slo_target_s,
            sizing=sizing,
            simulation=sim,
            slo_verdict=verdict,
            frontier=frontier,
            recommended_label=knee.label if knee else None,
        )
