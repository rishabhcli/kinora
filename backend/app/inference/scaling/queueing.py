"""Queue-theory sizing for the inference fleet (kinora.md §4.9, §12.2).

The autoscaler's question is "how many warm workers does backend X need *right
now* to keep its SLO?". That is a queueing question, and the reliability toolkit
already ships the textbook M/M/c machinery (:func:`app.reliability.capacity.erlang_c`,
:func:`~app.reliability.capacity.mmc_queue`). This module is the thin layer that
turns *those* primitives into the two numbers the scaler actually wants:

* the **latency-budget server count** — the smallest ``c`` such that the M/M/c
  queue's mean *response* time (wait + service) stays under a latency target, which
  is the SLO-aware sizing rule (vs. the utilisation rule the reliability toolkit
  already has);
* the **tail-latency server count** — the smallest ``c`` such that the *percentile*
  wait (not the mean) clears the target, because SLOs are written on p95/p99, and a
  mean-sized fleet blows its tail under bursty arrivals.

The percentile of the M/M/c waiting time is closed-form: conditional on waiting,
the wait is exponential with rate ``c·μ − λ``, so the unconditional ``p``-quantile
is ``W_p = max(0, (1/(cμ−λ))·ln(P_wait / (1−p)))``. We expose that as
:func:`mmc_wait_quantile_s` and size against it.

All pure arithmetic over the reliability primitives — deterministic, infra-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.reliability.capacity import QueueingResult, erlang_c, mmc_queue

__all__ = [
    "FleetSizing",
    "mmc_wait_quantile_s",
    "mmc_response_quantile_s",
    "servers_for_response_target",
    "servers_for_tail_target",
    "size_fleet",
]


def mmc_wait_quantile_s(
    *, arrival_rate_per_s: float, service_time_s: float, servers: int, quantile: float
) -> float:
    """The ``quantile``-th percentile *queue wait* of an M/M/c queue (seconds).

    Closed form: with ``μ = 1/service_time`` and ``λ = arrival_rate``, the
    waiting-time CDF is ``1 − P_wait·exp(−(cμ−λ)·t)``. Inverting for the quantile
    ``p`` gives ``W_p = max(0, ln(P_wait/(1−p)) / (cμ−λ))`` — zero when the chance
    of any wait is already below ``1−p`` (the queue clears the percentile with no
    wait at all). Returns ``inf`` for an unstable queue.
    """
    if not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be in (0, 1)")
    if servers <= 0:
        raise ValueError("servers must be positive")
    if service_time_s <= 0.0:
        raise ValueError("service_time_s must be positive")
    mu = 1.0 / service_time_s
    rate = servers * mu - arrival_rate_per_s
    if rate <= 0.0:
        return math.inf
    offered = arrival_rate_per_s * service_time_s
    p_wait = erlang_c(servers, offered)
    tail = 1.0 - quantile
    if p_wait <= tail:
        # The probability of *any* wait is already under the tail mass: the
        # quantile is served without waiting.
        return 0.0
    return math.log(p_wait / tail) / rate


def mmc_response_quantile_s(
    *, arrival_rate_per_s: float, service_time_s: float, servers: int, quantile: float
) -> float:
    """The ``quantile`` *response* time (wait + service) of an M/M/c queue.

    A pragmatic upper bound: the percentile wait plus the mean service time. Exact
    for the wait component; the service term is a constant offset (deterministic
    here since we model per-request service as its mean), which is the standard
    sizing approximation.
    """
    wq = mmc_wait_quantile_s(
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        servers=servers,
        quantile=quantile,
    )
    return wq + service_time_s


def servers_for_response_target(
    *,
    arrival_rate_per_s: float,
    service_time_s: float,
    target_response_s: float,
    max_servers: int = 256,
) -> int:
    """Smallest ``c`` whose M/M/c *mean response* time is ≤ ``target_response_s``.

    The SLO-aware analogue of
    :func:`app.reliability.capacity.min_servers_for_utilisation`: it sizes against
    a latency budget, not a utilisation ceiling. Raises if no ``c ≤ max_servers``
    can hit the target (the target is below the irreducible service time).
    """
    if target_response_s <= 0.0:
        raise ValueError("target_response_s must be positive")
    if target_response_s < service_time_s:
        raise ValueError(
            f"target_response_s ({target_response_s}) below service_time_s "
            f"({service_time_s}); unachievable for any server count"
        )
    floor = max(1, math.ceil(arrival_rate_per_s * service_time_s))
    for c in range(floor, max_servers + 1):
        result = mmc_queue(
            arrival_rate_per_s=arrival_rate_per_s,
            service_time_s=service_time_s,
            servers=c,
        )
        if result.stable and result.mean_response_s <= target_response_s:
            return c
    raise ValueError(
        f"no server count <= {max_servers} meets mean-response target "
        f"{target_response_s}s at lambda={arrival_rate_per_s}/s"
    )


def servers_for_tail_target(
    *,
    arrival_rate_per_s: float,
    service_time_s: float,
    target_response_s: float,
    quantile: float = 0.99,
    max_servers: int = 256,
) -> int:
    """Smallest ``c`` whose ``quantile`` response time is ≤ ``target_response_s``.

    SLOs live on the tail (p95/p99), so this is the sizing rule the autoscaler
    uses when an SLO is percentile-shaped. Strictly ≥ the mean-response count.
    """
    if target_response_s < service_time_s:
        raise ValueError(
            f"target_response_s ({target_response_s}) below service_time_s "
            f"({service_time_s}); unachievable for any server count"
        )
    floor = max(1, math.ceil(arrival_rate_per_s * service_time_s))
    for c in range(floor, max_servers + 1):
        resp = mmc_response_quantile_s(
            arrival_rate_per_s=arrival_rate_per_s,
            service_time_s=service_time_s,
            servers=c,
            quantile=quantile,
        )
        if resp <= target_response_s:
            return c
    raise ValueError(
        f"no server count <= {max_servers} meets p{int(quantile * 100)} target "
        f"{target_response_s}s at lambda={arrival_rate_per_s}/s"
    )


@dataclass(frozen=True, slots=True)
class FleetSizing:
    """The sizing verdict for one backend at one demand level (§12.2)."""

    servers: int
    arrival_rate_per_s: float
    service_time_s: float
    target_response_s: float
    quantile: float
    #: The M/M/c estimate at the chosen server count.
    queueing: QueueingResult
    #: The percentile response time the chosen count delivers.
    achieved_tail_s: float

    @property
    def meets_target(self) -> bool:
        """Whether the chosen count clears the tail target (always True post-size)."""
        return self.achieved_tail_s <= self.target_response_s

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the capacity report."""
        return {
            "servers": self.servers,
            "arrival_rate_per_s": round(self.arrival_rate_per_s, 5),
            "service_time_s": round(self.service_time_s, 3),
            "target_response_s": round(self.target_response_s, 3),
            "quantile": self.quantile,
            "achieved_tail_s": round(self.achieved_tail_s, 3),
            "meets_target": self.meets_target,
            "utilisation": round(self.queueing.utilisation, 4),
        }


def size_fleet(
    *,
    arrival_rate_per_s: float,
    service_time_s: float,
    target_response_s: float,
    quantile: float = 0.99,
    max_servers: int = 256,
) -> FleetSizing:
    """Size a backend to a tail-latency SLO and return the full verdict.

    The single entry point the autoscaler + the capacity report call: pick the
    smallest server count whose ``quantile`` response clears ``target_response_s``,
    then report the M/M/c estimate + achieved tail at that count.
    """
    servers = servers_for_tail_target(
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        target_response_s=target_response_s,
        quantile=quantile,
        max_servers=max_servers,
    )
    queueing = mmc_queue(
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        servers=servers,
    )
    achieved = mmc_response_quantile_s(
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        servers=servers,
        quantile=quantile,
    )
    return FleetSizing(
        servers=servers,
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        target_response_s=target_response_s,
        quantile=quantile,
        queueing=queueing,
        achieved_tail_s=achieved,
    )
