"""SLO-driven request routing across backends (kinora.md §12, §4.4).

When a model is served by *several* heterogeneous backends (a cheap-slow L20 pool
and a dear-fast H20 pool, plus a spot pool), the router must choose *which* one a
request goes to. The naive choices are wrong: round-robin ignores that the H20 is
faster; cheapest-first piles everything onto the L20 and blows the latency SLO under
load. The right policy is **SLO-aware, cost-minimising selection**: among the
backends that can still serve the request *within its latency budget*, pick the
cheapest; only reach for the expensive fast tier when the cheap tier would miss.

This module is that selector. It scores each candidate backend from the router's
live telemetry (facet A) — projecting the request's expected completion latency
from the backend's current queue depth + service time (an M/M/c-flavoured estimate)
— and applies a deterministic policy:

* drop UNHEALTHY backends, deprioritise DEGRADED;
* among backends whose *projected* tail latency clears the request's SLO budget,
  pick the **cheapest per request** (cost↔latency: spend the minimum that still
  meets the SLO);
* if none clears the budget, pick the **fastest** (the SLO-rescue tier) — the
  request is at risk, so minimise its latency rather than its cost;
* committed-zone requests (§4.4) bias toward the fast tier even with headroom,
  because the buffer is sacred; speculative requests are cost-first.

Pure given a metrics snapshot + a backend catalog; no I/O, no model calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendHealth,
    BackendId,
    BackendTelemetry,
)
from app.inference.scaling.instances import InstanceType
from app.inference.scaling.queueing import mmc_response_quantile_s
from app.inference.scaling.workload import RequestPriority

__all__ = [
    "RoutingCandidate",
    "RoutingPolicy",
    "RoutingDecision",
    "SLORouter",
]


@dataclass(frozen=True, slots=True)
class RoutingCandidate:
    """A backend the router may select, with its static + live state.

    Couples the descriptor (capacity), the instance type (cost/speed), and the
    current telemetry (queue depth, health) — everything the score needs.
    """

    descriptor: BackendDescriptor
    instance: InstanceType
    telemetry: BackendTelemetry

    @property
    def backend_id(self) -> BackendId:
        return self.descriptor.backend_id

    @property
    def effective_service_s(self) -> float:
        """Per-request latency this backend delivers (instance speed applied)."""
        return self.instance.effective_service_time_s(self.descriptor.service_time_s)

    def projected_tail_s(self, *, quantile: float) -> float:
        """Projected ``quantile`` completion latency given the current queue.

        Treats the warm fleet as an M/M/c with the *observed* offered rate
        approximated from the in-flight + queued work over the service time —
        a fast, dependency-free latency estimate from the live snapshot.
        """
        warm_slots = max(1, self.telemetry.warm_workers * self.descriptor.concurrency)
        service = self.effective_service_s
        # Estimate the offered rate that would produce the observed backlog at this
        # service time: (inflight + queue_depth) jobs draining at warm capacity.
        backlog = self.telemetry.inflight + self.telemetry.queue_depth
        # Offered load that keeps the queue at `backlog`: lambda ~= backlog/service
        # bounded below warm capacity so the estimate is stable.
        cap_rate = warm_slots / service
        offered = min(cap_rate * 0.999, backlog / service) if backlog > 0 else 0.0
        return mmc_response_quantile_s(
            arrival_rate_per_s=max(0.0, offered),
            service_time_s=service,
            servers=warm_slots,
            quantile=quantile,
        )

    def cost_per_request(self) -> float:
        """Marginal cost of serving one request on this backend."""
        return self.instance.cost_per_request(self.descriptor.service_time_s)


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Knobs for the SLO-aware selector."""

    #: The latency budget (seconds) a request must complete within to "meet SLO".
    target_tail_s: float = 60.0
    tail_quantile: float = 0.95
    #: Committed requests prefer the fastest backend even when the cheap one fits.
    committed_prefers_fast: bool = True
    #: A DEGRADED backend's projected latency is inflated by this factor (penalty).
    degraded_penalty: float = 1.5

    def __post_init__(self) -> None:
        if self.target_tail_s <= 0.0:
            raise ValueError("target_tail_s must be positive")
        if not 0.0 < self.tail_quantile < 1.0:
            raise ValueError("tail_quantile must be in (0, 1)")
        if self.degraded_penalty < 1.0:
            raise ValueError("degraded_penalty must be >= 1")


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The router's choice for one request (or a no-route signal)."""

    backend_id: BackendId | None
    priority: RequestPriority
    projected_tail_s: float
    cost_per_request: float
    met_slo: bool
    reason: str
    #: The scored candidates considered (for the report / debugging), cheapest first.
    considered: tuple[tuple[BackendId, float, float], ...] = field(default_factory=tuple)

    @property
    def routed(self) -> bool:
        """True when a backend was selected (vs. no healthy candidate)."""
        return self.backend_id is not None

    def to_dict(self) -> dict[str, object]:
        """JSON projection."""
        return {
            "backend_id": self.backend_id,
            "priority": self.priority.value,
            "projected_tail_s": round(self.projected_tail_s, 3),
            "cost_per_request": round(self.cost_per_request, 6),
            "met_slo": self.met_slo,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SLORouter:
    """SLO-aware, cost-minimising backend selector (the §12 routing brain)."""

    policy: RoutingPolicy = field(default_factory=RoutingPolicy)

    def route(
        self, *, candidates: list[RoutingCandidate], priority: RequestPriority
    ) -> RoutingDecision:
        """Select the backend for one request of the given zone ``priority``.

        Filters out unhealthy backends, scores the rest by projected tail latency +
        cost, then applies the policy: cheapest-that-meets-SLO (speculative) or
        fastest-that-meets-SLO (committed when ``committed_prefers_fast``); falls
        back to the globally fastest backend when none meets the budget.
        """
        routable = [
            c for c in candidates if c.telemetry.health is not BackendHealth.UNHEALTHY
        ]
        if not routable:
            return RoutingDecision(
                backend_id=None,
                priority=priority,
                projected_tail_s=math.inf,
                cost_per_request=math.inf,
                met_slo=False,
                reason="no healthy backend available",
            )

        scored: list[tuple[RoutingCandidate, float, float]] = []
        for c in routable:
            tail = c.projected_tail_s(quantile=self.policy.tail_quantile)
            if c.telemetry.health is BackendHealth.DEGRADED:
                tail *= self.policy.degraded_penalty
            scored.append((c, tail, c.cost_per_request()))

        within_budget = [s for s in scored if s[1] <= self.policy.target_tail_s]
        considered = tuple(
            sorted(((c.backend_id, t, cost) for c, t, cost in scored), key=lambda x: x[2])
        )

        prefer_fast = priority is RequestPriority.COMMITTED and self.policy.committed_prefers_fast

        if within_budget:
            if prefer_fast:
                chosen, tail, cost = min(within_budget, key=lambda s: s[1])
                reason = "committed: fastest backend within SLO budget"
            else:
                chosen, tail, cost = min(within_budget, key=lambda s: s[2])
                reason = "cheapest backend within SLO budget"
            return RoutingDecision(
                backend_id=chosen.backend_id,
                priority=priority,
                projected_tail_s=tail,
                cost_per_request=cost,
                met_slo=True,
                reason=reason,
                considered=considered,
            )

        # Nothing meets the budget: rescue with the globally fastest backend.
        chosen, tail, cost = min(scored, key=lambda s: s[1])
        return RoutingDecision(
            backend_id=chosen.backend_id,
            priority=priority,
            projected_tail_s=tail,
            cost_per_request=cost,
            met_slo=False,
            reason="SLO at risk: routed to fastest backend (none within budget)",
            considered=considered,
        )
