"""Discrete-event simulation harness for the scaling brain (kinora.md §12, §13).

This is the validation engine: a single-server-priority-queue *discrete-event
simulation* (DES) that drives the whole facet — the heterogeneous worker pool, the
predictive autoscaler, priority preemption, and load-shedding — against a workload
profile, then reports **SLO attainment** (latency percentiles, success rate) and
**cost** (provisioned instance-seconds, cold-start tax, idle tax). It is how a
capacity plan is *proven* before it touches real cloud capacity (§13's eval-harness
spirit, applied to the fleet rather than the film).

The engine is a textbook event-driven loop over a time-ordered priority queue of
events — no wall-clock, no threads, fully deterministic given the workload seed:

```
ARRIVAL      → admit/shed (LoadShedder); if admitted, try to dispatch or queue
DISPATCH     → assign to a servable worker (preempt a speculative victim if a
               committed request finds the fleet full); schedule its COMPLETION
COMPLETION   → free the slot, record latency; pull the next queued job (committed
               first); maybe the slot frees a preempted job to re-run
WORKER_READY → a WARMING worker finished cold-start → WARM → pull queued work
AUTOSCALE    → periodic tick: feed observed demand to the autoscaler, launch/drain
               workers toward the desired count (cold-start scheduled as
               WORKER_READY)
RECLAIM      → a spot worker is reclaimed → its in-flight jobs re-queue
```

Latency is accumulated into the reliability toolkit's :class:`LatencyDigest` so
the result plugs straight into :mod:`app.reliability.slo` for the pass/fail
verdict. Cost rolls up from the pool's per-worker accrual. The whole run is a pure
function of *(scenario, seed)* → :class:`SimulationResult`.
"""

from __future__ import annotations

import heapq
import itertools
import random
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

from app.core.logging import get_logger
from app.inference.scaling.autoscaler import PredictiveAutoscaler, ScalingPolicy
from app.inference.scaling.contracts import BackendDescriptor
from app.inference.scaling.forecast import EwmaForecaster, Forecaster
from app.inference.scaling.instances import CostBreakdown, InstanceType
from app.inference.scaling.pool import WorkerPool
from app.inference.scaling.preemption import (
    InflightJob,
    PreemptionPlanner,
)
from app.inference.scaling.shedding import LoadShedder
from app.inference.scaling.workload import Arrival, ArrivalGenerator, LoadProfile, RequestPriority
from app.reliability.latency import LatencyDigest, LatencySummary

logger = get_logger("app.inference.scaling.simulator")

__all__ = [
    "EventKind",
    "SimulationConfig",
    "SimulationResult",
    "FleetSimulator",
]


class EventKind(IntEnum):
    """Event types, ordered so ties at the same sim-time resolve deterministically.

    Completions/readies/reclaims process *before* arrivals at the same instant so
    freed capacity is available to a co-timed arrival; autoscale ticks last.
    """

    COMPLETION = 0
    WORKER_READY = 1
    RECLAIM = 2
    ARRIVAL = 3
    AUTOSCALE = 4


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Everything one simulation run needs (pure inputs → reproducible result)."""

    descriptor: BackendDescriptor
    instance: InstanceType
    scaling_policy: ScalingPolicy
    profile: LoadProfile
    horizon_s: float
    #: SLO budget: a request meeting its latency budget counts as "on SLO".
    slo_target_s: float = 60.0
    committed_fraction: float = 0.4
    autoscale_interval_s: float = 15.0
    #: Demand-forecast horizon (steps) fed to the autoscaler each tick.
    forecast_horizon: int = 1
    seed: int = 0
    #: Enable spot reclaims if the instance is a spot type.
    model_reclaims: bool = True


@dataclass
class _Job:
    """A request in flight in the sim (arrival → dispatch → completion)."""

    job_id: str
    priority: RequestPriority
    arrived_at: float
    total_s: float
    started_at: float | None = None
    worker_id: int | None = None
    #: Service already done before a preemption/reclaim (re-queued work).
    prior_progress_s: float = 0.0

    def elapsed_at(self, now: float) -> float:
        if self.started_at is None:
            return self.prior_progress_s
        return self.prior_progress_s + (now - self.started_at)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """The outcome of one run: SLO attainment + cost (the capacity-plan proof)."""

    arrivals: int
    admitted: int
    shed: int
    completed: int
    preemptions: int
    reclaims: int
    slo_met: int  # completed requests that met the latency budget
    wasted_compute_s: float  # service-seconds thrown away by preempt/reclaim
    latency: LatencySummary
    committed_latency: LatencySummary
    speculative_latency: LatencySummary
    cost: CostBreakdown
    horizon_s: float
    slo_target_s: float
    peak_warm_workers: int

    @property
    def shed_rate(self) -> float:
        return self.shed / self.arrivals if self.arrivals else 0.0

    @property
    def completion_rate(self) -> float:
        """Fraction of *admitted* requests that completed within the horizon."""
        return self.completed / self.admitted if self.admitted else 0.0

    @property
    def slo_attainment(self) -> float:
        """Fraction of *completed* requests that met the latency budget."""
        return self.slo_met / self.completed if self.completed else 1.0

    @property
    def cost_per_completed(self) -> float:
        import math

        return self.cost.total_cost / self.completed if self.completed else math.inf

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the capacity report."""
        return {
            "arrivals": self.arrivals,
            "admitted": self.admitted,
            "shed": self.shed,
            "shed_rate": round(self.shed_rate, 4),
            "completed": self.completed,
            "completion_rate": round(self.completion_rate, 4),
            "preemptions": self.preemptions,
            "reclaims": self.reclaims,
            "slo_attainment": round(self.slo_attainment, 4),
            "wasted_compute_s": round(self.wasted_compute_s, 3),
            "peak_warm_workers": self.peak_warm_workers,
            "horizon_s": round(self.horizon_s, 1),
            "slo_target_s": self.slo_target_s,
            "latency": self.latency.to_dict(),
            "committed_latency": self.committed_latency.to_dict(),
            "speculative_latency": self.speculative_latency.to_dict(),
            "cost": self.cost.to_dict(),
        }


@dataclass(order=True)
class _Event:
    """A scheduled event in the DES priority queue (ordered by time, kind, seq)."""

    time: float
    kind: int
    seq: int
    payload: object = field(compare=False, default=None)


class FleetSimulator:
    """Drives the scaling brain through a workload and reports SLO + cost.

    One instance owns the mutable run state (the event heap, the pool, the queues,
    the digests). :meth:`run` executes the whole horizon and returns a
    :class:`SimulationResult`. Reusable: each :meth:`run` resets state, so the same
    simulator can sweep many configs (the Pareto/report sweeps do this).
    """

    def __init__(
        self,
        config: SimulationConfig,
        *,
        forecaster: Forecaster | None = None,
        preemption: PreemptionPlanner | None = None,
        shedder: LoadShedder | None = None,
    ) -> None:
        self.config = config
        self._forecaster = forecaster or EwmaForecaster(alpha=0.4)
        self._preemption = preemption or PreemptionPlanner()
        self._shedder = shedder or LoadShedder(seed=config.seed)

    # ------------------------------------------------------------------ #
    # The run
    # ------------------------------------------------------------------ #

    def run(self) -> SimulationResult:
        """Execute the full horizon and return SLO attainment + cost."""
        cfg = self.config
        rng = random.Random(cfg.seed)
        heap: list[_Event] = []
        seq = itertools.count()
        pool = WorkerPool()
        autoscaler = PredictiveAutoscaler(
            cfg.descriptor, cfg.scaling_policy, current=cfg.scaling_policy.min_workers
        )

        # Priority-ordered waiting queues (committed served before speculative).
        committed_q: deque[_Job] = deque()
        speculative_q: deque[_Job] = deque()
        # job_id → _Job for the in-flight set (for preemption + reclaim).
        inflight: dict[str, _Job] = {}

        # Counters + digests.
        latency = LatencyDigest()
        committed_lat = LatencyDigest()
        speculative_lat = LatencyDigest()
        n_arrivals = n_admitted = n_shed = n_completed = 0
        n_preempt = n_reclaim = n_slo_met = 0
        wasted_s = 0.0
        peak_warm = 0
        # Per-interval admitted count → demand signal fed to the forecaster.
        interval_admits = 0
        last_autoscale_t = 0.0

        def push(time: float, kind: EventKind, payload: object = None) -> None:
            heapq.heappush(heap, _Event(time, int(kind), next(seq), payload))

        # Seed the warm-pool floor (min_workers) at t=0 so a returning reader is
        # served warm — they finish cold-start at their ready_at.
        for _ in range(cfg.scaling_policy.min_workers):
            w = pool.launch(instance=cfg.instance, now=0.0)
            push(w.ready_at, EventKind.WORKER_READY, w.worker_id)

        # Pre-generate the arrival stream (deterministic NHPP).
        arrivals = ArrivalGenerator(
            profile=cfg.profile,
            horizon_s=cfg.horizon_s,
            committed_fraction=cfg.committed_fraction,
            seed=cfg.seed,
        ).collect()
        for a in arrivals:
            push(a.t, EventKind.ARRIVAL, a)

        # First autoscale tick.
        push(cfg.autoscale_interval_s, EventKind.AUTOSCALE, None)

        # -------------------------------------------------------------- #
        # Helpers closing over the run state
        # -------------------------------------------------------------- #

        def service_time() -> float:
            """A jittered per-request service time for this backend/instance."""
            base = cfg.instance.effective_service_time_s(cfg.descriptor.service_time_s)
            # Exponential service (M/M/c assumption) keyed off the run RNG.
            return rng.expovariate(1.0 / base) if base > 0 else 0.0

        def try_dispatch(job: _Job, now: float) -> bool:
            """Place ``job`` on a free slot if one exists; schedule its completion."""
            worker = pool.pick_servable()
            if worker is None:
                return False
            worker.start_request(now)
            job.started_at = now
            job.worker_id = worker.worker_id
            inflight[job.job_id] = job
            remaining = max(0.0, job.total_s - job.prior_progress_s)
            push(now + remaining, EventKind.COMPLETION, job.job_id)
            return True

        def enqueue(job: _Job) -> None:
            if job.priority is RequestPriority.COMMITTED:
                committed_q.append(job)
            else:
                speculative_q.append(job)

        def pull_next() -> _Job | None:
            """Next queued job, committed first (priority discipline)."""
            if committed_q:
                return committed_q.popleft()
            if speculative_q:
                return speculative_q.popleft()
            return None

        def drain_queues(now: float) -> None:
            """Dispatch as many queued jobs as there are free slots."""
            while pool.free_slots > 0:
                job = pull_next()
                if job is None:
                    return
                if not try_dispatch(job, now):
                    # Shouldn't happen (free_slots>0), but re-queue defensively.
                    enqueue(job)
                    return

        def saturation() -> float:
            slots = max(1, pool.warm_count * cfg.descriptor.concurrency)
            return min(1.0, pool.inflight / slots)

        # -------------------------------------------------------------- #
        # Event loop
        # -------------------------------------------------------------- #

        while heap:
            ev = heapq.heappop(heap)
            now = ev.time
            kind = EventKind(ev.kind)

            # Bring any cold-started workers online lazily at each event so freed/
            # warmed capacity is reflected before this event is handled.
            pool.promote_ready(now)
            peak_warm = max(peak_warm, pool.warm_count)

            if kind is EventKind.ARRIVAL:
                arrival: Arrival = ev.payload  # type: ignore[assignment]
                n_arrivals += 1
                can_serve = pool.free_slots > 0
                decision = self._shedder.admit(
                    priority=arrival.priority,
                    saturation=saturation(),
                    outstanding=len(inflight) + len(committed_q) + len(speculative_q),
                    can_serve_now=can_serve,
                )
                if not decision.admitted:
                    n_shed += 1
                    continue
                n_admitted += 1
                interval_admits += 1
                job = _Job(
                    job_id=f"job-{n_arrivals}",
                    priority=arrival.priority,
                    arrived_at=now,
                    total_s=service_time(),
                )
                if not try_dispatch(job, now):
                    # Fleet full. A committed arrival may preempt a speculative victim.
                    placed = self._maybe_preempt(
                        job=job,
                        now=now,
                        inflight=inflight,
                        pool=pool,
                        descriptor=cfg.descriptor,
                        committed_q=committed_q,
                        speculative_q=speculative_q,
                    )
                    if placed is not None:
                        n_preempt += 1
                        wasted_s += placed
                        try_dispatch(job, now)
                    else:
                        enqueue(job)

            elif kind is EventKind.COMPLETION:
                done_id: str = ev.payload  # type: ignore[assignment]
                done = inflight.pop(done_id, None)
                if done is None:
                    continue  # already preempted/reclaimed
                worker = pool.workers.get(done.worker_id) if done.worker_id else None
                if worker is not None:
                    worker.finish_request(now)
                lat_s = now - done.arrived_at
                latency.record_s(lat_s)
                if done.priority is RequestPriority.COMMITTED:
                    committed_lat.record_s(lat_s)
                else:
                    speculative_lat.record_s(lat_s)
                n_completed += 1
                if lat_s <= cfg.slo_target_s:
                    n_slo_met += 1
                drain_queues(now)

            elif kind is EventKind.WORKER_READY:
                drain_queues(now)

            elif kind is EventKind.RECLAIM:
                victim_worker_id: int = ev.payload  # type: ignore[assignment]
                wasted_s += self._reclaim_worker(
                    worker_id=victim_worker_id,
                    now=now,
                    pool=pool,
                    inflight=inflight,
                    requeue=enqueue,
                )
                n_reclaim += 1
                drain_queues(now)

            elif kind is EventKind.AUTOSCALE:
                # Demand observed over the interval (admits → req/s) feeds forecast.
                interval = max(1e-9, now - last_autoscale_t)
                observed_rps = interval_admits / interval
                self._forecaster.observe(observed_rps)
                fc = self._forecaster.forecast(cfg.forecast_horizon)
                d = autoscaler.decide(forecast=fc, observed_warm=pool.warm_count)
                self._converge_pool(
                    desired=d.desired, pool=pool, instance=cfg.instance, now=now,
                    push=push, rng=rng, model_reclaims=cfg.model_reclaims,
                )
                interval_admits = 0
                last_autoscale_t = now
                if now + cfg.autoscale_interval_s <= cfg.horizon_s:
                    push(now + cfg.autoscale_interval_s, EventKind.AUTOSCALE, None)
                drain_queues(now)

        # Final cost snapshot at the horizon.
        final_t = cfg.horizon_s
        pool.accrue_all(final_t)
        cost = CostBreakdown(
            provisioned_cost=pool.total_cost(final_t),
            served_requests=n_completed,
            window_s=final_t,
            by_instance_type=pool.cost_by_instance_type(final_t),
            cold_start_cost=pool.cold_start_cost(final_t),
            idle_cost=pool.idle_cost(final_t),
        )

        return SimulationResult(
            arrivals=n_arrivals,
            admitted=n_admitted,
            shed=n_shed,
            completed=n_completed,
            preemptions=n_preempt,
            reclaims=n_reclaim,
            slo_met=n_slo_met,
            wasted_compute_s=wasted_s,
            latency=latency.summary(),
            committed_latency=committed_lat.summary(),
            speculative_latency=speculative_lat.summary(),
            cost=cost,
            horizon_s=cfg.horizon_s,
            slo_target_s=cfg.slo_target_s,
            peak_warm_workers=peak_warm,
        )

    # ------------------------------------------------------------------ #
    # Sub-steps (kept as methods for clarity + isolated testing)
    # ------------------------------------------------------------------ #

    def _maybe_preempt(
        self,
        *,
        job: _Job,
        now: float,
        inflight: dict[str, _Job],
        pool: WorkerPool,
        descriptor: BackendDescriptor,
        committed_q: deque[_Job],
        speculative_q: deque[_Job],
    ) -> float | None:
        """Try to free a slot for ``job`` by preempting a speculative victim.

        Returns the wasted service-seconds if a victim was preempted (and a slot
        thus freed), else ``None`` (no preemption — caller should queue ``job``).
        """
        snapshot = [
            InflightJob(
                job_id=j.job_id,
                priority=j.priority,
                elapsed_s=j.elapsed_at(now),
                total_s=j.total_s,
            )
            for j in inflight.values()
        ]
        decision = self._preemption.plan(
            arrival_priority=job.priority,
            inflight=snapshot,
            has_free_slot=pool.free_slots > 0,
        )
        if not decision.preempted or decision.victim_id is None:
            return None
        victim = inflight.pop(decision.victim_id, None)
        if victim is None:
            return None
        worker = pool.workers.get(victim.worker_id) if victim.worker_id else None
        if worker is not None:
            worker.finish_request(now)  # free the slot
        # Re-queue the victim with its partial progress preserved (it re-runs).
        victim.prior_progress_s = victim.elapsed_at(now)
        victim.started_at = None
        victim.worker_id = None
        speculative_q.appendleft(victim)
        return decision.wasted_s

    def _reclaim_worker(
        self,
        *,
        worker_id: int,
        now: float,
        pool: WorkerPool,
        inflight: dict[str, _Job],
        requeue: object,
    ) -> float:
        """Reclaim a spot worker; re-queue its in-flight jobs. Returns wasted_s."""
        wasted = 0.0
        victims = [j for j in inflight.values() if j.worker_id == worker_id]
        for j in victims:
            inflight.pop(j.job_id)
            wasted += j.elapsed_at(now)
            j.prior_progress_s = j.elapsed_at(now)
            j.started_at = None
            j.worker_id = None
            requeue(j)  # type: ignore[operator]
        pool.terminate(worker_id, now=now)
        return wasted

    def _converge_pool(
        self,
        *,
        desired: int,
        pool: WorkerPool,
        instance: InstanceType,
        now: float,
        push: object,
        rng: random.Random,
        model_reclaims: bool,
    ) -> None:
        """Launch/drain workers toward ``desired`` warm workers.

        Scale-up launches WARMING workers (a WORKER_READY event lands at their
        ready_at, after the cold-start). Spot workers also get a RECLAIM event
        sampled from their hazard. Scale-down terminates the *youngest* warm
        workers immediately (idle ones first).
        """
        servable = pool.warm_count + pool.warming_count
        if desired > servable:
            for _ in range(desired - servable):
                w = pool.launch(instance=instance, now=now)
                push(w.ready_at, EventKind.WORKER_READY, w.worker_id)  # type: ignore[operator]
                if model_reclaims and instance.spot:
                    rate = instance.reclaim_hazard_per_s()
                    if rate > 0:
                        reclaim_at = now + rng.expovariate(rate)
                        push(reclaim_at, EventKind.RECLAIM, w.worker_id)  # type: ignore[operator]
        elif desired < servable:
            # Terminate idle warm workers first (least disruptive).
            removable = sorted(
                (w for w in pool.workers.values() if w.inflight == 0),
                key=lambda w: -w.provisioned_at,  # youngest idle first
            )
            for w in removable[: servable - desired]:
                pool.terminate(w.worker_id, now=now)
