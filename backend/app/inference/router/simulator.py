"""A deterministic discrete-event simulator for the inference router.

This is the validation harness the brief asks for: it drives the
:class:`~app.inference.router.router.InferenceRouter` with a synthetic,
*seeded* request stream over a virtual clock and a fake backend, then reports
fairness + throughput so we can assert — in a unit test, with zero wall-clock
waiting and zero live calls — that:

* a tenant with weight *w* gets ~*w*× the served work of a weight-1 tenant
  (weighted fair share holds);
* higher-priority work is served before lower (priority is respected);
* the queue stays bounded and backpressure sheds the right class;
* throughput (requests/virtual-second) scales with worker count.

Everything is injectable and deterministic: the clock is a virtual time the
simulator advances itself, the RNG is seeded, and :class:`SimBackend` returns
fixed token counts. No real time passes; ``asyncio`` is used only to await the
router's already-async API on a fresh event loop per ``run``.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import AdmissionRejected
from .fairshare import FairShareConfig
from .protocols import InferenceResult
from .request import InferenceRequest, RequestPriority, prefix_key_for
from .router import InferenceRouter, RouterConfig
from .worker import WorkerConfig, WorkerPool


class VirtualClock:
    """A clock the simulator advances by hand (monotonic, deterministic)."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("cannot advance time backwards")
        self._t += dt


class SimBackend:
    """A deterministic fake backend: echoes fixed token counts, never blocks.

    ``output_tokens`` may be a constant or a callable of the request, so a
    scenario can model variable generation lengths. Optionally fails a fraction
    of requests (seeded) to exercise the failure path.
    """

    def __init__(
        self,
        model: str,
        *,
        output_tokens: int = 64,
        fail_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._model = model
        self._output_tokens = output_tokens
        self._fail_keys = fail_keys
        self.executed_batches: list[int] = []

    @property
    def model(self) -> str:
        return self._model

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        self.executed_batches.append(len(requests))
        out: list[InferenceResult] = []
        for r in requests:
            if r.request_id in self._fail_keys:
                out.append(
                    InferenceResult(
                        request_id=r.request_id,
                        model=self._model,
                        output_tokens=0,
                        error="sim-fail",
                    )
                )
                continue
            out.append(
                InferenceResult(
                    request_id=r.request_id,
                    model=self._model,
                    output_tokens=min(
                        self._output_tokens, r.max_output_tokens or self._output_tokens
                    ),
                    prompt_tokens=r.prompt_tokens,
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class TenantSpec:
    """A synthetic tenant in a scenario."""

    name: str
    weight: float = 1.0
    request_share: float = 1.0
    priority: RequestPriority = RequestPriority.COMMITTED
    #: Number of distinct shared prefixes this tenant cycles through.
    prefix_count: int = 1
    prompt_tokens: int = 256
    max_output_tokens: int = 64


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    """A reproducible workload + topology for a simulation run.

    Attributes:
        arrivals_per_tick: How many requests arrive *before* each scheduling
            step. Greater than the per-tick dispatch capacity builds a standing
            backlog in which flows compete — which is the regime where weighted
            fair share (and priority) is actually exercised. ``1`` degenerates to
            an under-loaded system that dispatches everything immediately (useful
            for the no-rejection, all-complete scenarios).
        arrival_dt: Virtual seconds the clock advances per tick (for wait/SLA
            accounting + throughput denominator).
    """

    tenants: tuple[TenantSpec, ...]
    n_requests: int = 600
    n_workers: int = 2
    worker: WorkerConfig = field(
        default_factory=lambda: WorkerConfig(token_capacity=4096, max_slots=8)
    )
    router: RouterConfig = field(default_factory=RouterConfig)
    arrival_dt: float = 0.05
    arrivals_per_tick: int = 1
    #: Steady-state fairness mode. When set, *all* requests are admitted up front
    #: (building a full backlog), then exactly this many scheduling ticks run and
    #: served work is measured in that window — the rest stays queued. This is
    #: how weighted fair share is observed: when the run drains to completion,
    #: every flow is fully served and the *total* served work is equal regardless
    #: of weight; weight governs the *rate*, visible only mid-backlog. ``None``
    #: runs the interleaved arrive-then-drain-to-completion loop.
    measure_window_ticks: int | None = None
    seed: int = 1234

    def __post_init__(self) -> None:
        if not self.tenants:
            raise ValueError("scenario needs at least one tenant")
        if self.n_requests <= 0 or self.n_workers <= 0:
            raise ValueError("n_requests and n_workers must be positive")
        if self.arrivals_per_tick <= 0:
            raise ValueError("arrivals_per_tick must be positive")
        if self.measure_window_ticks is not None and self.measure_window_ticks <= 0:
            raise ValueError("measure_window_ticks must be positive when set")


@dataclass
class SimReport:
    """The measured outcome of a simulation run."""

    total_submitted: int = 0
    total_rejected: int = 0
    total_succeeded: int = 0
    total_failed: int = 0
    virtual_seconds: float = 0.0
    served_by_tenant: dict[str, int] = field(default_factory=dict)
    served_cost_by_tenant: dict[str, float] = field(default_factory=dict)
    served_by_priority: dict[str, int] = field(default_factory=dict)
    stats_snapshot: dict[str, object] = field(default_factory=dict)

    @property
    def throughput_rps(self) -> float:
        """Succeeded requests per virtual second."""
        return self.total_succeeded / self.virtual_seconds if self.virtual_seconds else 0.0

    def fairness_ratio(self, tenant_a: str, tenant_b: str) -> float:
        """Served-work ratio A/B (compare against the weight ratio)."""
        a = self.served_cost_by_tenant.get(tenant_a, 0.0)
        b = self.served_cost_by_tenant.get(tenant_b, 0.0)
        return a / b if b else float("inf")


class RouterSimulator:
    """Builds a router from a :class:`ScenarioConfig` and runs it to completion."""

    def __init__(self, scenario: ScenarioConfig) -> None:
        self.scenario = scenario
        self.clock = VirtualClock()
        self._rng = random.Random(scenario.seed)
        self.backend = SimBackend(model="sim-model")
        self.router = self._build_router()

    def _build_router(self) -> InferenceRouter:
        pool = WorkerPool("sim-model")
        for i in range(self.scenario.n_workers):
            pool.add_configured_worker(f"w{i}", self.scenario.worker)
        # Fold tenant weights into the fair-share config so the router honours them.
        base = self.scenario.router
        weights = {t.name: t.weight for t in self.scenario.tenants}
        fs = FairShareConfig(
            default_weight=base.fairshare.default_weight,
            tenant_weights={**base.fairshare.tenant_weights, **weights},
            flow_weights=base.fairshare.flow_weights,
        )
        config = RouterConfig(
            admission=base.admission,
            fairshare=fs,
            affinity=base.affinity,
            prefill_chunk_budget=base.prefill_chunk_budget,
            default_queue_sla_s=base.default_queue_sla_s,
            coalescing_enabled=base.coalescing_enabled,
        )
        return InferenceRouter("sim-model", pool, self.backend, config=config, clock=self.clock)

    def _weighted_tenants(self) -> list[TenantSpec]:
        """Expand tenants into a sampling population by ``request_share``."""
        pop: list[TenantSpec] = []
        for t in self.scenario.tenants:
            pop.extend([t] * max(1, round(t.request_share * 10)))
        return pop

    def _make_request(self, idx: int, tenant: TenantSpec) -> InferenceRequest:
        prefix_idx = self._rng.randrange(max(1, tenant.prefix_count))
        prefix = prefix_key_for(f"{tenant.name}:prefix:{prefix_idx}")
        return InferenceRequest(
            request_id=f"{tenant.name}-{idx}",
            model="sim-model",
            tenant=tenant.name,
            agent=tenant.name,
            priority=tenant.priority,
            prompt_tokens=tenant.prompt_tokens,
            max_output_tokens=tenant.max_output_tokens,
            prefix_key=prefix,
        )

    async def _run_async(self) -> SimReport:
        if self.scenario.measure_window_ticks is not None:
            return await self._run_window()
        return await self._run_drain()

    async def _submit(self, idx: int, report: SimReport) -> asyncio.Future[InferenceResult] | None:
        tenant = self._rng.choice(self._weighted_tenants())
        req = self._make_request(idx, tenant)
        report.total_submitted += 1
        try:
            return await self.router.submit(req)
        except AdmissionRejected:
            report.total_rejected += 1
            return None

    async def _run_drain(self) -> SimReport:
        """Arrive bursts, drain to completion (everyone eventually served)."""
        report = SimReport()
        futures: list[asyncio.Future[InferenceResult]] = []
        per_tick = self.scenario.arrivals_per_tick
        n = self.scenario.n_requests
        idx = 0
        while idx < n:
            for _ in range(per_tick):
                if idx >= n:
                    break
                fut = await self._submit(idx, report)
                idx += 1
                if fut is not None:
                    futures.append(fut)
            self.clock.advance(self.scenario.arrival_dt)
            await self.router.tick()

        for _ in range(n * 8):
            dispatched = await self.router.tick()
            self.clock.advance(self.scenario.arrival_dt)
            if dispatched == 0 and self.router.queue_depth == 0:
                break
        self._finalize(report, futures)
        return report

    async def _run_window(self) -> SimReport:
        """Admit the full backlog up front, then serve a fixed measurement window.

        Served work is read mid-backlog, so the per-flow served-cost ratio
        reflects the *weights* (the steady-state fairness signal) rather than the
        total submitted work.
        """
        report = SimReport()
        futures: list[asyncio.Future[InferenceResult]] = []
        for idx in range(self.scenario.n_requests):
            fut = await self._submit(idx, report)
            if fut is not None:
                futures.append(fut)
        window = self.scenario.measure_window_ticks or 1
        for _ in range(window):
            await self.router.tick()
            self.clock.advance(self.scenario.arrival_dt)
        self._finalize(report, futures)
        return report

    def _finalize(self, report: SimReport, futures: list[asyncio.Future[InferenceResult]]) -> None:
        """Tally outcomes + per-flow served work into ``report`` (in place)."""
        for fut in futures:
            if not fut.done():
                continue
            exc = fut.exception()
            if exc is not None:
                report.total_failed += 1
                continue
            res = fut.result()
            if res.ok:
                report.total_succeeded += 1

        report.virtual_seconds = self.clock()
        served = self.router._queue.served_cost_by_flow()  # noqa: SLF001 - sim reads internals
        for (tenant_name, _agent), cost in served.items():
            report.served_cost_by_tenant[tenant_name] = (
                report.served_cost_by_tenant.get(tenant_name, 0.0) + cost
            )
        snap = self.router.stats.snapshot()
        report.stats_snapshot = dict(snap)
        served_by_pri = snap.get("served_by_priority")
        if isinstance(served_by_pri, dict):
            report.served_by_priority = dict(served_by_pri)
        report.served_by_tenant = self._served_count_by_tenant()

    def _served_count_by_tenant(self) -> dict[str, int]:
        """Dispatched-request counts per tenant (summed from per-flow WFQ counts)."""
        out: dict[str, int] = {}
        for (tenant_name, _agent), flows in self._flow_served_counts().items():
            out[tenant_name] = out.get(tenant_name, 0) + flows
        return out

    def _flow_served_counts(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for flows in self.router._queue._classes.values():  # noqa: SLF001 - sim introspection
            for key, flow in flows.items():
                counts[key] = counts.get(key, 0) + flow.served_count
        return counts

    def run(self) -> SimReport:
        """Run the scenario to completion on a fresh event loop; return the report."""
        return asyncio.run(self._run_async())


__all__ = [
    "RouterSimulator",
    "ScenarioConfig",
    "SimBackend",
    "SimReport",
    "TenantSpec",
    "VirtualClock",
]
