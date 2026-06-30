"""Deterministic autoscaler simulator — proves the controller beats fixed sizing.

A no-infra harness that replays a synthetic **demand trace** (a per-tick sequence
of :class:`~app.autoscale.signal.DemandSnapshot`) through the real
:class:`~app.autoscale.controller.RenderAutoscaler` and a tiny queueing model of
the lanes, scoring three things that matter for the product:

* **underrun rate** — fraction of ticks where the committed buffer was at risk and
  the serving lane could not keep up (the reader would stall). This is the metric
  the autoscaler exists to minimise.
* **idle-worker waste** — replica-ticks of warm capacity with no work (overspend).
* **scaling oscillation** — count + magnitude of scale direction reversals (flap).

It runs the same trace twice — once with the elastic controller, once against a
**static baseline** pinned at a fixed size — and reports both, so a test can assert
the controller wins (fewer underruns at comparable-or-lower waste, with bounded
flap). All deterministic: a trace + a :class:`~app.autoscale.clock.VirtualClock`
in, the same :class:`ScenarioComparison` out. Zero video, zero spend, zero infra.

Scenario generators (:func:`steady_trace`, :func:`spike_trace`, :func:`diurnal_trace`,
:func:`ingest_burst_trace`) synthesise the four canonical demand shapes from a
seed; :func:`default_scenarios` bundles them for a suite run.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.autoscale.clock import VirtualClock
from app.autoscale.controller import AutoscalerConfig, RenderAutoscaler
from app.autoscale.lanes import Lane, LanePool, QoSClass, default_lane_pools, lane_for_qos
from app.autoscale.signal import (
    DemandSnapshot,
    SessionDemand,
)
from app.core.logging import get_logger

logger = get_logger("app.autoscale.simulator")

__all__ = [
    "RunMetrics",
    "ScenarioComparison",
    "compare_scenario",
    "default_scenarios",
    "diurnal_trace",
    "ingest_burst_trace",
    "run_static",
    "run_trace",
    "spike_trace",
    "steady_trace",
]

#: Default seconds between simulated ticks (the §4.7 settle cadence ballpark).
DEFAULT_TICK_S = 5.0
#: How many jobs one warm replica drains per tick in the queueing model.
DEFAULT_DRAIN_PER_REPLICA = 2.0


# --------------------------------------------------------------------------- #
# Demand-trace generators (deterministic, seedable, no randomness imports).
# --------------------------------------------------------------------------- #


def _det_jitter(seed: int, i: int, span: float) -> float:
    """A deterministic, bounded pseudo-jitter in ``[-span, span]`` (no RNG state)."""
    # A cheap reproducible hash-free wobble: a couple of detuned sinusoids.
    x = math.sin((i + 1) * 12.9898 + seed * 78.233) * 43758.5453
    frac = x - math.floor(x)  # in [0, 1)
    return (frac * 2.0 - 1.0) * span


def _snapshot(
    *,
    committed_depth: int,
    speculative_depth: int = 0,
    keyframe_depth: int = 0,
    provider_inflight: int = 0,
    cpu_inflight: int = 0,
    gpu_inflight: int = 0,
    p95_provider: float = 8.0,
    p95_cpu: float = 2.0,
    sessions: Sequence[SessionDemand] = (),
    provider_quota: int | None = 32,
) -> DemandSnapshot:
    return DemandSnapshot(
        depth_by_qos={
            QoSClass.COMMITTED: max(0, committed_depth),
            QoSClass.SPECULATIVE: max(0, speculative_depth),
            QoSClass.KEYFRAME: max(0, keyframe_depth),
        },
        inflight_by_lane={
            Lane.PROVIDER: max(0, provider_inflight),
            Lane.CPU: max(0, cpu_inflight),
            Lane.GPU: max(0, gpu_inflight),
        },
        latency_samples_s={
            Lane.PROVIDER: [p95_provider],
            Lane.CPU: [p95_cpu],
        },
        sessions=tuple(sessions),
        provider_quota=provider_quota,
    )


def steady_trace(
    ticks: int = 60, *, depth: int = 8, sessions: int = 6, seed: int = 1
) -> list[DemandSnapshot]:
    """Flat demand with small jitter — the controller should hold near baseline."""
    out: list[DemandSnapshot] = []
    for i in range(ticks):
        d = max(0, depth + int(_det_jitter(seed, i, 1.5)))
        sess = tuple(
            SessionDemand(velocity_wps=4.0, committed_seconds_ahead=45.0)
            for _ in range(sessions)
        )
        out.append(_snapshot(committed_depth=d, provider_inflight=min(d, 12), sessions=sess))
    return out


def spike_trace(
    ticks: int = 60, *, base: int = 4, peak: int = 22, seed: int = 2
) -> list[DemandSnapshot]:
    """A reader-velocity spike mid-run — tests predictive pre-warm + fast scale-out.

    The spike has a **velocity lead-in**: for a few ticks before the queue depth
    peaks, readers accelerate and their committed buffers drain, so aggregate
    underrun *risk* climbs while the realised queue is still small. A predictive
    controller warms capacity during that lead-in and is ready when the depth peaks;
    a purely reactive (or under-provisioned static) pool only reacts once the queue
    has already filled, and underruns through the peak.
    """
    out: list[DemandSnapshot] = []
    lead = ticks // 12  # velocity lead-in length (risk climbs, depth still low)
    spike_lo = ticks // 3
    spike_hi = spike_lo + ticks // 6
    for i in range(ticks):
        in_lead = (spike_lo - lead) <= i < spike_lo
        in_peak = spike_lo <= i < spike_hi
        if in_peak:
            depth = max(0, peak + int(_det_jitter(seed, i, 2.0)))
            sess = tuple(
                SessionDemand(velocity_wps=11.0, committed_seconds_ahead=6.0)
                for _ in range(8)
            )
            p95 = 18.0
        elif in_lead:
            # Depth still near base, but buffers already draining at high velocity.
            depth = base + 2
            sess = tuple(
                SessionDemand(velocity_wps=11.0, committed_seconds_ahead=10.0)
                for _ in range(8)
            )
            p95 = 10.0
        else:
            depth = max(0, base + int(_det_jitter(seed, i, 1.0)))
            sess = tuple(
                SessionDemand(velocity_wps=4.0, committed_seconds_ahead=45.0) for _ in range(4)
            )
            p95 = 8.0
        out.append(
            _snapshot(
                committed_depth=depth,
                provider_inflight=min(depth, 14),
                p95_provider=p95,
                sessions=sess,
            )
        )
    return out


def diurnal_trace(
    ticks: int = 96, *, low: int = 3, high: int = 40, seed: int = 3
) -> list[DemandSnapshot]:
    """A smooth sinusoidal day/night demand curve — tests graceful scale-in."""
    out: list[DemandSnapshot] = []
    mid = (high + low) / 2.0
    amp = (high - low) / 2.0
    for i in range(ticks):
        phase = 2.0 * math.pi * (i / ticks)
        depth = int(round(mid + amp * math.sin(phase) + _det_jitter(seed, i, 1.5)))
        depth = max(0, depth)
        # Sessions scale with demand; buffers stay healthy (no spike, just volume).
        n = max(1, int(round(depth / 4.0)))
        sess = tuple(
            SessionDemand(velocity_wps=4.5, committed_seconds_ahead=40.0) for _ in range(n)
        )
        out.append(
            _snapshot(committed_depth=depth, provider_inflight=min(depth, 14), sessions=sess)
        )
    return out


def ingest_burst_trace(
    ticks: int = 60, *, base: int = 5, burst: int = 80, seed: int = 4
) -> list[DemandSnapshot]:
    """A new-book ingest dumps a large keyframe/speculative backlog at once.

    Unlike the velocity spike this is *throughput* demand with no underrun risk
    (no reader is waiting on it), so the controller should scale-out on backlog but
    not pre-warm, and shed it quickly afterward.
    """
    out: list[DemandSnapshot] = []
    burst_at = ticks // 4
    for i in range(ticks):
        bursting = burst_at <= i < burst_at + 4
        spec = burst if bursting else 0
        keyf = (burst // 2) if bursting else base
        depth = base + int(_det_jitter(seed, i, 1.0))
        sess = tuple(
            SessionDemand(velocity_wps=4.0, committed_seconds_ahead=50.0) for _ in range(3)
        )
        out.append(
            _snapshot(
                committed_depth=max(0, depth),
                speculative_depth=max(0, spec),
                keyframe_depth=max(0, keyf),
                provider_inflight=min(spec + keyf, 14),
                sessions=sess,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Queueing model + metrics.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RunMetrics:
    """Scored outcome of one trace replay (controller or baseline)."""

    label: str
    ticks: int
    underrun_ticks: int = 0
    idle_replica_ticks: float = 0.0
    busy_replica_ticks: float = 0.0
    scale_events: int = 0
    direction_reversals: int = 0
    total_replica_delta: int = 0
    peak_replicas: int = 0
    final_replicas: dict[Lane, int] = field(default_factory=dict)
    cost_capped_ticks: int = 0

    @property
    def underrun_rate(self) -> float:
        return self.underrun_ticks / self.ticks if self.ticks else 0.0

    @property
    def idle_waste_rate(self) -> float:
        total = self.idle_replica_ticks + self.busy_replica_ticks
        return self.idle_replica_ticks / total if total else 0.0

    @property
    def oscillation(self) -> float:
        """Reversals per scale event — flap measure (0 = monotone, 1 = thrashing)."""
        return self.direction_reversals / self.scale_events if self.scale_events else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "ticks": self.ticks,
            "underrun_ticks": self.underrun_ticks,
            "underrun_rate": round(self.underrun_rate, 4),
            "idle_replica_ticks": round(self.idle_replica_ticks, 2),
            "idle_waste_rate": round(self.idle_waste_rate, 4),
            "scale_events": self.scale_events,
            "direction_reversals": self.direction_reversals,
            "oscillation": round(self.oscillation, 4),
            "peak_replicas": self.peak_replicas,
            "cost_capped_ticks": self.cost_capped_ticks,
            "final_replicas": {ln.value: n for ln, n in self.final_replicas.items()},
        }


@dataclass(slots=True)
class ScenarioComparison:
    """The headline result: controller metrics vs the static baseline."""

    scenario: str
    controller: RunMetrics
    baseline: RunMetrics

    @property
    def underrun_improvement(self) -> float:
        """Absolute reduction in underrun rate vs baseline (positive = better)."""
        return self.baseline.underrun_rate - self.controller.underrun_rate

    @property
    def waste_improvement(self) -> float:
        """Reduction in idle-waste rate vs baseline (positive = leaner)."""
        return self.baseline.idle_waste_rate - self.controller.idle_waste_rate

    def controller_wins(self) -> bool:
        """True when the controller underruns no more than baseline and isn't wasteful.

        The product contract: never stall the reader more than a fixed pool would,
        and don't pay for more idle capacity than the baseline's worst case.
        """
        return (
            self.controller.underrun_rate <= self.baseline.underrun_rate + 1e-9
            and self.controller.idle_waste_rate <= self.baseline.idle_waste_rate + 1e-9
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "underrun_improvement": round(self.underrun_improvement, 4),
            "waste_improvement": round(self.waste_improvement, 4),
            "controller_wins": self.controller_wins(),
            "controller": self.controller.as_dict(),
            "baseline": self.baseline.as_dict(),
        }


def _committed_lane() -> Lane:
    return lane_for_qos(QoSClass.COMMITTED)


def _is_underrun(snapshot: DemandSnapshot, serving_replicas: int) -> bool:
    """A tick underruns if a session is at real risk AND capacity can't drain it.

    Risk comes from the signal model (a near-dry buffer at velocity). The *imminent*
    work the readers are blocked on is the realised committed queue depth; the
    capacity is the committed-serving lane's throughput this tick. A stall happens
    when at least one session is meaningfully at risk and the warm pool cannot clear
    the committed backlog before the buffer dries. The forward look-ahead drives
    *scaling* (so the pool is large by the time the spike lands); it is not itself a
    stall — only an un-drainable realised queue at risk is.
    """
    risk = snapshot.total_underrun_risk()
    if risk < 0.5:  # no session meaningfully at risk this tick
        return False
    committed_depth = snapshot.depth_by_qos.get(QoSClass.COMMITTED, 0)
    capacity = serving_replicas * DEFAULT_DRAIN_PER_REPLICA
    return capacity < committed_depth


def run_trace(
    trace: Sequence[DemandSnapshot],
    *,
    label: str = "controller",
    pools: dict[Lane, LanePool] | None = None,
    config: AutoscalerConfig | None = None,
    tick_s: float = DEFAULT_TICK_S,
) -> RunMetrics:
    """Replay ``trace`` through the elastic controller, scoring it tick by tick."""
    pools = pools or default_lane_pools()
    config = config or AutoscalerConfig()
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(pools=pools, config=config, clock=clock)
    committed_lane = _committed_lane()

    metrics = RunMetrics(label=label, ticks=len(trace))
    last_direction = 0
    for snapshot in trace:
        plan = autoscaler.plan(snapshot)
        sizes = autoscaler.current

        # Scoring (against the *post-decision* capacity for this tick).
        serving = sizes.get(committed_lane, 0)
        if _is_underrun(snapshot, serving):
            metrics.underrun_ticks += 1

        # Idle vs busy replica-ticks per lane.
        for lane, n in sizes.items():
            inflight = snapshot.inflight_by_lane.get(lane, 0)
            depth = 0
            for qos, d in snapshot.depth_by_qos.items():
                if lane_for_qos(qos) == lane:
                    depth += d
            busy = min(n, inflight + depth)
            metrics.busy_replica_ticks += busy
            metrics.idle_replica_ticks += max(0, n - busy)

        total = sum(sizes.values())
        metrics.peak_replicas = max(metrics.peak_replicas, total)
        if plan.cost_capped:
            metrics.cost_capped_ticks += 1

        # Flap accounting on the net replica delta.
        net_delta = sum(d.delta for d in plan.decisions.values())
        if net_delta != 0:
            metrics.scale_events += 1
            metrics.total_replica_delta += abs(net_delta)
            direction = 1 if net_delta > 0 else -1
            if last_direction != 0 and direction != last_direction:
                metrics.direction_reversals += 1
            last_direction = direction

        clock.advance(tick_s)

    metrics.final_replicas = dict(autoscaler.current)
    return metrics


def run_static(
    trace: Sequence[DemandSnapshot],
    *,
    label: str = "baseline",
    sizes: dict[Lane, int] | None = None,
    pools: dict[Lane, LanePool] | None = None,
) -> RunMetrics:
    """Score a *fixed-size* pool against the same trace (the comparison baseline).

    The baseline is pinned: no scaling, ever. Its size defaults to the §4.9
    steady-state caps (each lane's minimum) — a fair "what an engineer would set
    statically" reference.
    """
    pools = pools or default_lane_pools()
    if sizes is None:
        sizes = {lane: pool.min_replicas for lane, pool in pools.items()}
    committed_lane = _committed_lane()

    metrics = RunMetrics(label=label, ticks=len(trace))
    for snapshot in trace:
        serving = sizes.get(committed_lane, 0)
        if _is_underrun(snapshot, serving):
            metrics.underrun_ticks += 1
        for lane, n in sizes.items():
            inflight = snapshot.inflight_by_lane.get(lane, 0)
            depth = sum(
                d for qos, d in snapshot.depth_by_qos.items() if lane_for_qos(qos) == lane
            )
            busy = min(n, inflight + depth)
            metrics.busy_replica_ticks += busy
            metrics.idle_replica_ticks += max(0, n - busy)
        metrics.peak_replicas = max(metrics.peak_replicas, sum(sizes.values()))
    metrics.final_replicas = dict(sizes)
    return metrics


def compare_scenario(
    scenario: str,
    trace: Sequence[DemandSnapshot],
    *,
    pools: dict[Lane, LanePool] | None = None,
    config: AutoscalerConfig | None = None,
    baseline_sizes: dict[Lane, int] | None = None,
    tick_s: float = DEFAULT_TICK_S,
) -> ScenarioComparison:
    """Run the controller and the static baseline on one trace and compare."""
    pools = pools or default_lane_pools()
    controller = run_trace(
        trace, label="controller", pools=pools, config=config, tick_s=tick_s
    )
    # A fair static baseline: size it to the controller's *peak* committed lane so
    # the baseline has the headroom to match underruns — the only way to undercut
    # it is on idle waste, which is exactly the controller's win.
    if baseline_sizes is None:
        baseline_sizes = {
            lane: max(pool.min_replicas, controller.peak_replicas // max(1, len(pools)))
            for lane, pool in pools.items()
        }
    baseline = run_static(trace, label="baseline", sizes=baseline_sizes, pools=pools)
    comp = ScenarioComparison(scenario=scenario, controller=controller, baseline=baseline)
    logger.info(
        "autoscale.sim.compare",
        scenario=scenario,
        underrun_improvement=round(comp.underrun_improvement, 4),
        waste_improvement=round(comp.waste_improvement, 4),
        wins=comp.controller_wins(),
    )
    return comp


def default_scenarios() -> dict[str, list[DemandSnapshot]]:
    """The four canonical demand shapes, ready for a suite comparison."""
    return {
        "steady": steady_trace(),
        "spike": spike_trace(),
        "diurnal": diurnal_trace(),
        "ingest_burst": ingest_burst_trace(),
    }
