"""Throughput / latency / cost aggregation for a serving-simulation run.

The simulator produces a list of completed (and any failed) requests with
timestamps plus a small set of run-level counters. This module turns that into the
report a capacity planner actually reads:

* **Latency** — per-request time-to-first-token (TTFT) and end-to-end latency, plus
  their p50/p90/p99 percentiles. TTFT is queue-wait + prefill; end-to-end adds all
  the decode steps.
* **Throughput** — tokens/sec and requests/sec over the wall-clock span of the run.
* **Cost** — total tokens × per-1k-token price, and cost per request.
* **Efficiency** — mean batch occupancy, KV-cache utilization, and the speculative
  speedup actually realized.

Percentiles use the nearest-rank method on the sorted sample so they are exact and
deterministic (no interpolation surprises). Everything is pure arithmetic over the
already-simulated numbers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.mlplatform.serving.requests import InferenceRequest, RequestState


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of ``values`` for ``q`` in ``[0, 100]``.

    Returns ``0.0`` for an empty sample. Deterministic and interpolation-free.
    """
    if not 0.0 <= q <= 100.0:
        raise ValueError("q must be in [0, 100]")
    if not values:
        return 0.0
    ordered = sorted(values)
    if q <= 0:
        return ordered[0]
    if q >= 100:
        return ordered[-1]
    # nearest-rank: rank = ceil(q/100 * N), 1-based
    rank = math.ceil((q / 100.0) * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Percentile summary of one latency series (milliseconds)."""

    mean: float
    p50: float
    p90: float
    p99: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> LatencyStats:
        if not values:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0)
        return cls(
            mean=_mean(values),
            p50=percentile(values, 50),
            p90=percentile(values, 90),
            p99=percentile(values, 99),
            max=max(values),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "mean_ms": round(self.mean, 3),
            "p50_ms": round(self.p50, 3),
            "p90_ms": round(self.p90, 3),
            "p99_ms": round(self.p99, 3),
            "max_ms": round(self.max, 3),
        }


@dataclass(frozen=True, slots=True)
class ServingReport:
    """The full capacity-planning summary of a simulation run."""

    n_completed: int
    n_failed: int
    wall_clock_ms: float
    total_prompt_tokens: int
    total_generated_tokens: int
    ttft: LatencyStats
    e2e: LatencyStats
    tokens_per_s: float
    requests_per_s: float
    total_cost: float
    cost_per_request: float
    mean_batch_occupancy: float
    peak_batch_occupancy: int
    mean_kv_utilization: float
    peak_kv_utilization: float
    kv_reuse_ratio: float
    speculative_speedup: float
    sim_steps: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "n_completed": self.n_completed,
            "n_failed": self.n_failed,
            "wall_clock_ms": round(self.wall_clock_ms, 3),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_generated_tokens": self.total_generated_tokens,
            "ttft": self.ttft.as_dict(),
            "e2e": self.e2e.as_dict(),
            "tokens_per_s": round(self.tokens_per_s, 3),
            "requests_per_s": round(self.requests_per_s, 3),
            "total_cost": round(self.total_cost, 6),
            "cost_per_request": round(self.cost_per_request, 6),
            "mean_batch_occupancy": round(self.mean_batch_occupancy, 3),
            "peak_batch_occupancy": self.peak_batch_occupancy,
            "mean_kv_utilization": round(self.mean_kv_utilization, 4),
            "peak_kv_utilization": round(self.peak_kv_utilization, 4),
            "kv_reuse_ratio": round(self.kv_reuse_ratio, 4),
            "speculative_speedup": round(self.speculative_speedup, 4),
            "sim_steps": self.sim_steps,
        }


@dataclass(slots=True)
class RunAccumulator:
    """Mutable per-step counters the simulator feeds; folded into a report at end."""

    batch_occupancy_samples: list[int] = field(default_factory=list)
    kv_utilization_samples: list[float] = field(default_factory=list)
    sim_steps: int = 0

    def observe_step(self, batch_size: int, kv_utilization: float) -> None:
        self.batch_occupancy_samples.append(batch_size)
        self.kv_utilization_samples.append(kv_utilization)
        self.sim_steps += 1


def summarize_run(
    completed: Sequence[InferenceRequest],
    failed: Sequence[InferenceRequest],
    *,
    cost_per_1k_tokens: float,
    wall_clock_ms: float,
    accumulator: RunAccumulator | None = None,
    kv_reuse_ratio: float = 0.0,
    speculative_speedup: float = 1.0,
) -> ServingReport:
    """Fold the simulator's output into a :class:`ServingReport`."""
    ttft_samples: list[float] = []
    e2e_samples: list[float] = []
    total_prompt = 0
    total_gen = 0
    for r in completed:
        total_prompt += r.prompt_tokens
        total_gen += r.generated
        if r.first_token_ms is not None:
            ttft_samples.append(r.first_token_ms - r.arrival_ms)
        if r.finish_ms is not None:
            e2e_samples.append(r.finish_ms - r.arrival_ms)
    total_tokens = total_prompt + total_gen
    span_s = wall_clock_ms / 1000.0 if wall_clock_ms > 0 else 0.0
    tokens_per_s = total_tokens / span_s if span_s > 0 else 0.0
    requests_per_s = len(completed) / span_s if span_s > 0 else 0.0
    total_cost = (total_tokens / 1000.0) * cost_per_1k_tokens
    cost_per_request = total_cost / len(completed) if completed else 0.0

    acc = accumulator or RunAccumulator()
    occ = acc.batch_occupancy_samples
    util = acc.kv_utilization_samples
    return ServingReport(
        n_completed=len(completed),
        n_failed=len(failed),
        wall_clock_ms=wall_clock_ms,
        total_prompt_tokens=total_prompt,
        total_generated_tokens=total_gen,
        ttft=LatencyStats.of(ttft_samples),
        e2e=LatencyStats.of(e2e_samples),
        tokens_per_s=tokens_per_s,
        requests_per_s=requests_per_s,
        total_cost=total_cost,
        cost_per_request=cost_per_request,
        mean_batch_occupancy=_mean([float(x) for x in occ]),
        peak_batch_occupancy=max(occ) if occ else 0,
        mean_kv_utilization=_mean(util),
        peak_kv_utilization=max(util) if util else 0.0,
        kv_reuse_ratio=kv_reuse_ratio,
        speculative_speedup=speculative_speedup,
        sim_steps=acc.sim_steps,
    )


def _failed_terminal(requests: Sequence[InferenceRequest]) -> tuple[InferenceRequest, ...]:
    """Partition helper used by tests: the FAILED subset of a request list."""
    return tuple(r for r in requests if r.state == RequestState.FAILED)


__all__ = [
    "LatencyStats",
    "RunAccumulator",
    "ServingReport",
    "percentile",
    "summarize_run",
]
