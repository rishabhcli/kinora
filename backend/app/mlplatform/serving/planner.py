"""Capacity planner: sweep serving configs to find a throughput/latency/cost frontier.

Given a fixed workload and a model profile, the question a platform operator asks is
*"how should I configure the server?"* — what batch size, token budget, KV-cache
size, and whether to turn on prefix reuse or speculative decoding. This module runs
the discrete-event simulator across a grid of :class:`SimConfig`s and returns the
results ranked, so the operator can read the trade-off off a single table instead of
guessing.

It also exposes :func:`recommend`, a small policy that picks a config meeting a
latency SLO at the lowest cost (or, if none meets it, the lowest-latency config) —
the kind of decision a serving controller makes automatically.

Pure and deterministic: every candidate is simulated with the same seeded workload,
so the ranking is reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.mlplatform.serving.batching import ContinuousBatchConfig
from app.mlplatform.serving.errors import ServingConfigError
from app.mlplatform.serving.kvcache import PagedKVConfig
from app.mlplatform.serving.metrics import ServingReport
from app.mlplatform.serving.model import ModelProfile
from app.mlplatform.serving.requests import InferenceRequest
from app.mlplatform.serving.simulator import ServingSimulator, SimConfig
from app.mlplatform.serving.speculative import SpeculativeConfig


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    """One simulated configuration and its resulting report."""

    label: str
    config: SimConfig
    report: ServingReport

    def as_dict(self) -> dict[str, object]:
        return {"label": self.label, "report": self.report.as_dict()}


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """The full sweep result, candidates ranked by an objective."""

    candidates: tuple[PlanCandidate, ...]
    objective: str

    def best(self) -> PlanCandidate:
        if not self.candidates:
            raise ServingConfigError("capacity plan has no candidates")
        return self.candidates[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class SweepGrid:
    """The grid of knobs to sweep. Each list is a dimension; the product is explored.

    Sizes are kept modest by default so a sweep stays fast and deterministic — this
    is a planning tool, not an exhaustive search.
    """

    batch_sizes: tuple[int, ...] = (4, 8, 16)
    token_budgets: tuple[int, ...] = (4096, 8192)
    cache_blocks: tuple[int, ...] = (512, 1024)
    block_tokens: int = 16
    max_admit_per_step: int = 8
    prefix_keys: tuple[str | None, ...] = (None,)
    speculative: tuple[SpeculativeConfig, ...] = (SpeculativeConfig(enabled=False),)

    def __post_init__(self) -> None:
        if not self.batch_sizes or not self.token_budgets or not self.cache_blocks:
            raise ServingConfigError("sweep grid dimensions must be non-empty")


_OBJECTIVES = ("tokens_per_s", "p99_latency", "cost", "cost_per_token")


def _objective_key(report: ServingReport, objective: str) -> float:
    """A sort key where *smaller is better* (so we can always sort ascending)."""
    if objective == "tokens_per_s":
        return -report.tokens_per_s  # maximize throughput
    if objective == "p99_latency":
        return report.e2e.p99
    if objective == "cost":
        return report.total_cost
    if objective == "cost_per_token":
        total = report.total_prompt_tokens + report.total_generated_tokens
        return report.total_cost / total if total else float("inf")
    raise ServingConfigError(f"unknown objective {objective!r}; pick one of {_OBJECTIVES}")


class CapacityPlanner:
    """Runs a config sweep over one workload and ranks the outcomes."""

    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile

    def _candidate_configs(self, grid: SweepGrid) -> list[tuple[str, SimConfig]]:
        configs: list[tuple[str, SimConfig]] = []
        for blocks in grid.cache_blocks:
            cache_token_capacity = blocks * grid.block_tokens
            for budget in grid.token_budgets:
                if budget > cache_token_capacity:
                    continue  # invalid: SimConfig would reject it
                for bs in grid.batch_sizes:
                    for prefix in grid.prefix_keys:
                        for spec in grid.speculative:
                            label = (
                                f"bs={bs},budget={budget},blocks={blocks},"
                                f"prefix={'on' if prefix else 'off'},"
                                f"spec={'on' if spec.enabled else 'off'}"
                            )
                            cfg = SimConfig(
                                profile=self.profile,
                                cache=PagedKVConfig(
                                    total_blocks=blocks, block_tokens=grid.block_tokens
                                ),
                                batch=ContinuousBatchConfig(
                                    max_batch_size=bs,
                                    max_batch_tokens=budget,
                                    max_admit_per_step=grid.max_admit_per_step,
                                ),
                                speculative=spec,
                                shared_prefix_key=prefix,
                            )
                            configs.append((label, cfg))
        if not configs:
            raise ServingConfigError(
                "no valid configs in the sweep grid — check token budgets vs. cache size"
            )
        return configs

    def sweep(
        self,
        workload: Sequence[InferenceRequest],
        grid: SweepGrid | None = None,
        *,
        objective: str = "tokens_per_s",
    ) -> CapacityPlan:
        """Simulate every grid config over ``workload`` and rank by ``objective``."""
        grid = grid or SweepGrid()
        reqs = list(workload)
        candidates: list[PlanCandidate] = []
        for label, cfg in self._candidate_configs(grid):
            report = ServingSimulator(cfg).run(reqs)
            candidates.append(PlanCandidate(label=label, config=cfg, report=report))
        # Deterministic ranking: objective, then label as a stable tiebreak.
        candidates.sort(key=lambda c: (_objective_key(c.report, objective), c.label))
        return CapacityPlan(candidates=tuple(candidates), objective=objective)

    def recommend(
        self,
        workload: Sequence[InferenceRequest],
        *,
        p99_latency_slo_ms: float,
        grid: SweepGrid | None = None,
    ) -> PlanCandidate:
        """Pick the cheapest config meeting a p99 end-to-end latency SLO.

        If no config meets the SLO, return the one with the lowest p99 latency (the
        closest feasible answer) — the planner never returns nothing.
        """
        plan = self.sweep(workload, grid, objective="cost")
        feasible = [c for c in plan.candidates if c.report.e2e.p99 <= p99_latency_slo_ms]
        if feasible:
            # plan.candidates is already cost-sorted, so the first feasible is cheapest.
            return feasible[0]
        # Fall back to the lowest-latency candidate.
        return min(plan.candidates, key=lambda c: c.report.e2e.p99)


__all__ = [
    "CapacityPlan",
    "CapacityPlanner",
    "PlanCandidate",
    "SweepGrid",
]
