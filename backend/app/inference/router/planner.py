"""Offline batch planner — pack a bulk workload into token-budget batches (§11.1).

The §11.1 budget rule sends all non-realtime work through the **batch API
(~50% off)**: Phase-A page analysis, bulk keyframe generation, offline
re-scoring. Unlike the live router (which dispatches continuously as a reader
arrives), an offline job is a *fixed set* of requests to be packed into the
fewest token-budget-bounded micro-batches — a pure planning problem with no
clock, no workers, no fair share.

:class:`BatchPlanner` is that planner, built directly on the
:class:`~app.inference.router.binpack.TokenBinPacker`: it repeatedly packs the
remaining requests into a batch of a configured token + slot budget, optionally
**grouping by prefix first** so a batch shares a warm prefix (the same
KV-affinity win the live router gets, applied to offline batching). Oversized
requests (bigger than a whole batch budget) are reported separately so the
caller can split or reject them. Deterministic and side-effect-free, so the
batch plan is reproducible and testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .binpack import BatchBudget, TokenBinPacker
from .errors import RouterConfigError
from .request import InferenceRequest


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Tunables for offline batch planning.

    Attributes:
        token_budget: Max worst-case token footprint per batch.
        slot_budget: Max requests per batch.
        group_by_prefix: Pack requests sharing a ``prefix_key`` into the same
            batch first (a warm-prefix batch), falling back to mixed batches for
            the leftovers. ``False`` packs in arrival order.
    """

    token_budget: int = 8192
    slot_budget: int = 32
    group_by_prefix: bool = True

    def __post_init__(self) -> None:
        if self.token_budget <= 0:
            raise RouterConfigError("token_budget must be positive")
        if self.slot_budget <= 0:
            raise RouterConfigError("slot_budget must be positive")


@dataclass(slots=True)
class BatchPlan:
    """The result of planning: a list of batches + the un-packable oversized set."""

    batches: list[list[InferenceRequest]] = field(default_factory=list)
    oversized: list[InferenceRequest] = field(default_factory=list)

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    @property
    def total_packed(self) -> int:
        return sum(len(b) for b in self.batches)

    def fill_ratios(self, token_budget: int) -> list[float]:
        """Per-batch token fill ratio — how full each batch is (planning quality)."""
        return [
            sum(r.total_tokens for r in batch) / token_budget if token_budget else 0.0
            for batch in self.batches
        ]


class BatchPlanner:
    """Greedy offline planner over the token bin-packer."""

    def __init__(self, config: PlannerConfig | None = None) -> None:
        self.config = config or PlannerConfig()
        self._packer = TokenBinPacker()

    def plan(self, requests: Sequence[InferenceRequest]) -> BatchPlan:
        """Pack ``requests`` into batches honouring the token + slot budgets."""
        plan = BatchPlan()
        groups = self._grouped(requests)
        for group in groups:
            remaining: list[InferenceRequest] = list(group)
            while remaining:
                budget = BatchBudget(
                    token_budget=self.config.token_budget,
                    slot_budget=self.config.slot_budget,
                )
                packed = self._packer.pack(remaining, budget)
                plan.oversized.extend(packed.oversized)
                if packed.batch:
                    plan.batches.append(packed.batch)
                # Continue with what deferred (didn't fit *this* batch).
                next_remaining = packed.deferred
                if not packed.batch and not next_remaining:
                    break  # only oversized remained
                if not packed.batch:
                    # Nothing fit but deferred is non-empty — shouldn't happen
                    # (a request that fits the empty budget always packs first),
                    # but guard against an infinite loop.
                    break
                remaining = next_remaining
        return plan

    def _grouped(self, requests: Sequence[InferenceRequest]) -> list[list[InferenceRequest]]:
        """Optionally bucket requests by prefix key (warm-prefix batching)."""
        if not self.config.group_by_prefix:
            return [list(requests)]
        buckets: dict[str | None, list[InferenceRequest]] = {}
        order: list[str | None] = []
        for req in requests:
            if req.prefix_key not in buckets:
                buckets[req.prefix_key] = []
                order.append(req.prefix_key)
            buckets[req.prefix_key].append(req)
        return [buckets[k] for k in order]


__all__ = ["BatchPlan", "BatchPlanner", "PlannerConfig"]
