"""Heterogeneous GPU/worker instance types + the cost model (kinora.md §11, §12.2).

The render fleet is not one homogeneous pool. On Alibaba Cloud (the §11 deploy
target) the same Wan model can be served on a spread of GPU instance families that
trade **cost against speed**: a small L20-class instance is cheap but slow; a
big H20/A10-class instance is fast but dear; a spot/preemptible instance is the
cheapest of all but can be reclaimed mid-flight. The autoscaler and the Pareto
optimiser need a faithful, *pure* model of that trade so they can pick the cheapest
fleet that still clears the SLO.

This module is that model. An :class:`InstanceType` carries:

* its **speed** — a ``service_time_multiplier`` applied to a backend's nominal
  per-request latency (``< 1`` = faster than nominal, ``> 1`` = slower),
* its **cost** — billed per *second of provisioned wall-clock* (the cloud charges
  for the instance whether or not it is serving, which is exactly why scale-to-zero
  and warm-pools matter),
* its **cold-start** — the seconds from "ask for an instance" to "first request
  served", the single most important number for scale-to-zero (§12.2): you pay
  the cold-start latency every time you scale up from zero,
* its **reliability** — a spot/preemptible flag + a reclaim hazard the simulator
  uses to inject mid-flight loss.

All arithmetic, all deterministic. The default catalog encodes a realistic L20 /
A10 / H20 / spot spread so the Pareto frontier has something to chew on; a
deployment overrides it from config without touching code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "BillingModel",
    "InstanceType",
    "CostBreakdown",
    "DEFAULT_CATALOG",
    "default_catalog",
    "catalog_by_name",
]


class BillingModel(StrEnum):
    """How a cloud charges for a provisioned instance."""

    #: Charged per second the instance exists, serving or idle (the common case).
    PER_SECOND = "per_second"
    #: Charged per second but only while actively serving a request (FaaS-like).
    PER_REQUEST_SECOND = "per_request_second"


@dataclass(frozen=True, slots=True)
class InstanceType:
    """One heterogeneous hardware option in the fleet catalog.

    ``service_time_multiplier`` scales a backend's nominal latency: a fast H20 at
    ``0.5`` halves it; a slow L20 at ``1.6`` lengthens it. ``cost_per_hour`` is the
    provisioned (not per-request) hourly rate; we derive a per-second rate from it.
    ``cold_start_s`` is the time-to-first-served on a scale-up from zero — paid
    once per instance launch. Spot instances set ``spot=True`` + a non-zero
    ``reclaim_hazard_per_hour`` the simulator turns into mid-flight reclaims.
    """

    name: str
    cost_per_hour: float
    service_time_multiplier: float = 1.0
    cold_start_s: float = 30.0
    #: Max concurrent requests a single instance serves (GPU memory bound).
    max_concurrency: int = 1
    billing: BillingModel = BillingModel.PER_SECOND
    spot: bool = False
    #: Poisson hazard rate of a spot reclaim, in events/hour (0 = on-demand).
    reclaim_hazard_per_hour: float = 0.0
    #: Relative output-quality weight (1.0 = nominal); the optimiser can prefer it.
    quality: float = 1.0
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cost_per_hour < 0.0:
            raise ValueError("cost_per_hour must be non-negative")
        if self.service_time_multiplier <= 0.0:
            raise ValueError("service_time_multiplier must be positive")
        if self.cold_start_s < 0.0:
            raise ValueError("cold_start_s must be non-negative")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.reclaim_hazard_per_hour < 0.0:
            raise ValueError("reclaim_hazard_per_hour must be non-negative")
        if self.spot and self.reclaim_hazard_per_hour <= 0.0:
            raise ValueError("a spot instance must declare a positive reclaim hazard")

    @property
    def cost_per_second(self) -> float:
        """Provisioned cost per wall-clock second."""
        return self.cost_per_hour / 3600.0

    def effective_service_time_s(self, nominal_service_time_s: float) -> float:
        """The per-request latency this instance delivers for a nominal backend."""
        return nominal_service_time_s * self.service_time_multiplier

    def throughput_per_s(self, nominal_service_time_s: float) -> float:
        """Requests/second one warm instance of this type clears for a backend."""
        eff = self.effective_service_time_s(nominal_service_time_s)
        return self.max_concurrency / eff if eff > 0 else math.inf

    def cost_per_request(self, nominal_service_time_s: float) -> float:
        """Marginal cost of one request served by a *busy* instance.

        For ``PER_REQUEST_SECOND`` billing this is the only charge; for
        ``PER_SECOND`` billing it is the slice of provisioned cost the request
        occupies (cost/s × its effective service time ÷ concurrency).
        """
        eff = self.effective_service_time_s(nominal_service_time_s)
        return self.cost_per_second * eff / self.max_concurrency

    def reclaim_hazard_per_s(self) -> float:
        """Poisson reclaim rate per second (0 for on-demand)."""
        return self.reclaim_hazard_per_hour / 3600.0

    def survival_probability(self, seconds: float) -> float:
        """P(a spot instance is *not* reclaimed within ``seconds``) — exp survival."""
        if seconds < 0.0:
            raise ValueError("seconds must be non-negative")
        rate = self.reclaim_hazard_per_s()
        if rate <= 0.0:
            return 1.0
        return math.exp(-rate * seconds)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """A decomposed cost estimate over a simulation/planning window (§11)."""

    provisioned_cost: float  # paid for instance-seconds (warm + idle + cold-start)
    served_requests: int
    window_s: float
    #: Per-instance-type provisioned cost (for the capacity report breakdown).
    by_instance_type: dict[str, float] = field(default_factory=dict)
    #: Cost paid purely for cold-start ramp (the scale-to-zero penalty, §12.2).
    cold_start_cost: float = 0.0
    #: Cost paid for idle warm-pool capacity held for latency (§4.5 readiness).
    idle_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        """Total provisioned cost over the window."""
        return self.provisioned_cost

    @property
    def cost_per_request(self) -> float:
        """Amortised cost per served request (``inf`` if nothing served)."""
        if self.served_requests <= 0:
            return math.inf
        return self.provisioned_cost / self.served_requests

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the capacity report."""
        return {
            "provisioned_cost": round(self.provisioned_cost, 6),
            "served_requests": self.served_requests,
            "window_s": round(self.window_s, 3),
            "cost_per_request": (
                None if self.served_requests <= 0 else round(self.cost_per_request, 6)
            ),
            "cold_start_cost": round(self.cold_start_cost, 6),
            "idle_cost": round(self.idle_cost, 6),
            "by_instance_type": {
                k: round(v, 6) for k, v in sorted(self.by_instance_type.items())
            },
        }


# --------------------------------------------------------------------------- #
# The default heterogeneous catalog (realistic Alibaba-style GPU spread)
# --------------------------------------------------------------------------- #


def default_catalog() -> dict[str, InstanceType]:
    """A realistic heterogeneous GPU catalog for the Pareto/sim defaults (§11).

    Four families spanning the cost/speed/reliability trade:

    * ``gpu-l20`` — cheap, slow, slow cold-start: the budget baseline.
    * ``gpu-a10`` — mid cost/speed, the workhorse.
    * ``gpu-h20`` — dear, fast, fast cold-start: the burst/SLO-rescue tier.
    * ``gpu-l20-spot`` — cheapest of all but reclaimable mid-flight.

    Numbers are *relative* and deterministic; a deployment overrides via config.
    """
    return {
        "gpu-l20": InstanceType(
            name="gpu-l20",
            cost_per_hour=1.20,
            service_time_multiplier=1.6,
            cold_start_s=45.0,
            max_concurrency=1,
            quality=1.0,
        ),
        "gpu-a10": InstanceType(
            name="gpu-a10",
            cost_per_hour=2.40,
            service_time_multiplier=1.0,
            cold_start_s=30.0,
            max_concurrency=2,
            quality=1.0,
        ),
        "gpu-h20": InstanceType(
            name="gpu-h20",
            cost_per_hour=6.00,
            service_time_multiplier=0.5,
            cold_start_s=20.0,
            max_concurrency=2,
            quality=1.05,
        ),
        "gpu-l20-spot": InstanceType(
            name="gpu-l20-spot",
            cost_per_hour=0.40,
            service_time_multiplier=1.6,
            cold_start_s=60.0,
            max_concurrency=1,
            spot=True,
            reclaim_hazard_per_hour=2.0,
            quality=1.0,
        ),
    }


#: The frozen default catalog (most callers want this; pass a custom dict to override).
DEFAULT_CATALOG: dict[str, InstanceType] = default_catalog()


def catalog_by_name(names: list[str]) -> dict[str, InstanceType]:
    """Subset the default catalog to ``names`` (raises on an unknown name)."""
    catalog = default_catalog()
    missing = [n for n in names if n not in catalog]
    if missing:
        raise KeyError(f"unknown instance type(s): {missing}")
    return {n: catalog[n] for n in names}
