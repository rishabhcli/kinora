"""Unit tests for cost-latency Pareto optimisation (app...scaling.pareto)."""

from __future__ import annotations

from app.inference.scaling.autoscaler import ScalingPolicy
from app.inference.scaling.instances import default_catalog
from app.inference.scaling.pareto import (
    FleetCandidate,
    ParetoPoint,
    ParetoSweep,
    default_candidates,
    dominates,
)
from app.inference.scaling.simulator import SimulationResult
from app.inference.scaling.workload import ConstantLoad, RampLoad
from app.reliability.latency import LatencyDigest


def _point(label: str, cost: float, lat: float, attain: float, feasible: bool) -> ParetoPoint:
    # A throwaway SimulationResult is needed for the dataclass; only summaries used.
    digest = LatencyDigest()
    digest.record_ms(lat)
    res = SimulationResult(
        arrivals=10, admitted=10, shed=0, completed=10, preemptions=0, reclaims=0,
        slo_met=int(attain * 10), wasted_compute_s=0.0,
        latency=digest.summary(), committed_latency=digest.summary(),
        speculative_latency=digest.summary(),
        cost=_zero_cost(cost), horizon_s=100.0, slo_target_s=60.0, peak_warm_workers=2,
    )
    return ParetoPoint(
        label=label, cost=cost, latency_p95_ms=lat, slo_attainment=attain,
        feasible=feasible, result=res,
    )


def _zero_cost(total: float):  # type: ignore[no-untyped-def]
    from app.inference.scaling.instances import CostBreakdown

    return CostBreakdown(provisioned_cost=total, served_requests=10, window_s=100.0)


# --------------------------------------------------------------------------- #
# Domination relation
# --------------------------------------------------------------------------- #


def test_dominates_when_better_on_both() -> None:
    a = _point("a", cost=1.0, lat=10.0, attain=1.0, feasible=True)
    b = _point("b", cost=2.0, lat=20.0, attain=1.0, feasible=True)
    assert dominates(a, b)
    assert not dominates(b, a)


def test_no_domination_on_a_trade() -> None:
    cheap_slow = _point("cs", cost=1.0, lat=20.0, attain=1.0, feasible=True)
    dear_fast = _point("df", cost=2.0, lat=10.0, attain=1.0, feasible=True)
    assert not dominates(cheap_slow, dear_fast)
    assert not dominates(dear_fast, cheap_slow)


def test_strict_improvement_required() -> None:
    a = _point("a", cost=1.0, lat=10.0, attain=1.0, feasible=True)
    b = _point("b", cost=1.0, lat=10.0, attain=1.0, feasible=True)
    assert not dominates(a, b)  # identical => no domination


# --------------------------------------------------------------------------- #
# Frontier construction via the sweep
# --------------------------------------------------------------------------- #


def test_sweep_builds_a_frontier() -> None:
    sweep = ParetoSweep(
        profile=ConstantLoad(0.6),
        horizon_s=400.0,
        slo_target_s=60.0,
        min_slo_attainment=0.8,
        seed=4,
    )
    candidates = default_candidates(target_tail_s=60.0)
    frontier = sweep.run(candidates)
    assert len(frontier.points) == len(candidates)
    assert len(frontier.frontier) >= 1
    # The frontier is non-dominated: no point dominates another.
    for p in frontier.frontier:
        for q in frontier.frontier:
            if p is not q:
                assert not dominates(q, p)


def test_frontier_sorted_by_cost() -> None:
    sweep = ParetoSweep(
        profile=ConstantLoad(0.6), horizon_s=400.0, slo_target_s=60.0,
        min_slo_attainment=0.8, seed=4,
    )
    frontier = sweep.run(default_candidates(target_tail_s=60.0))
    costs = [p.cost for p in frontier.frontier]
    assert costs == sorted(costs)


def test_cheapest_and_fastest_selectors() -> None:
    sweep = ParetoSweep(
        profile=ConstantLoad(0.7), horizon_s=500.0, slo_target_s=60.0,
        min_slo_attainment=0.8, seed=2,
    )
    frontier = sweep.run(default_candidates(target_tail_s=60.0))
    cheapest = frontier.cheapest()
    fastest = frontier.fastest()
    assert cheapest is not None and fastest is not None
    # The cheapest is no dearer than the fastest; the fastest is no slower.
    assert cheapest.cost <= fastest.cost
    assert fastest.latency_p95_ms <= cheapest.latency_p95_ms


def test_knee_is_on_the_frontier() -> None:
    sweep = ParetoSweep(
        profile=RampLoad(start_rate=0.2, end_rate=1.0, duration_s=500.0),
        horizon_s=500.0, slo_target_s=60.0, min_slo_attainment=0.8, seed=1,
    )
    frontier = sweep.run(default_candidates(target_tail_s=60.0))
    knee = frontier.knee()
    if knee is not None:
        assert knee in frontier.frontier


def test_infeasible_configs_excluded_from_frontier() -> None:
    # An impossibly tight SLO target makes everything infeasible.
    sweep = ParetoSweep(
        profile=ConstantLoad(2.0), horizon_s=300.0, slo_target_s=60.0,
        min_slo_attainment=1.01,  # impossible attainment floor
        seed=1,
    )
    frontier = sweep.run(default_candidates(target_tail_s=60.0))
    assert frontier.frontier == ()
    assert frontier.cheapest() is None
    assert frontier.knee() is None


def test_sweep_is_deterministic() -> None:
    def run() -> tuple[str, ...]:
        sweep = ParetoSweep(
            profile=ConstantLoad(0.6), horizon_s=400.0, slo_target_s=60.0,
            min_slo_attainment=0.8, seed=7,
        )
        f = sweep.run(default_candidates(target_tail_s=60.0))
        return tuple(p.label for p in f.frontier)

    assert run() == run()


def test_default_candidates_grid_size() -> None:
    cands = default_candidates(warm_pool_options=(0, 1, 2))
    # 4 instance types × 3 warm-pool depths.
    assert len(cands) == len(default_catalog()) * 3
    assert all(isinstance(c, FleetCandidate) for c in cands)


def test_candidate_descriptor_id_from_label() -> None:
    cat = default_catalog()
    c = FleetCandidate(
        label="h20×warm2", instance=cat["gpu-h20"],
        policy=ScalingPolicy(warm_pool=2), concurrency=2,
    )
    assert c.descriptor().backend_id == "sweep::h20×warm2"


def test_frontier_to_dict() -> None:
    sweep = ParetoSweep(
        profile=ConstantLoad(0.6), horizon_s=300.0, slo_target_s=60.0,
        min_slo_attainment=0.8, seed=5,
    )
    payload = sweep.run(default_candidates(target_tail_s=60.0)).to_dict()
    assert "frontier" in payload
    assert int(payload["evaluated"]) >= int(payload["feasible"])  # type: ignore[call-overload]
