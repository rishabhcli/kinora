"""Build the RANKED provider candidate list for a shot.

This is the planning half of the coordinator: take the providers the router knows,
prune the ones the capacity/SLA governor won't admit and the ones whose estimated
cost alone can't fit the remaining budget, then **rank** the survivors by a
weighted blend of quality reputation, governor load-headroom, and (cheaper-is-
better) estimated cost. Pure functions over the local Protocols — no I/O, fully
deterministic, ranking is a stable sort so ties keep the router's own order.

Pruned providers aren't silently dropped: the caller gets back the *reason* for
each pruned provider so it can write an honest :class:`AttemptRecord` (e.g.
``GOVERNOR_BLOCKED`` / ``SKIPPED_NO_BUDGET_HEADROOM``) into the attempt log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.video.reliability.config import ReliabilityConfig
from app.video.reliability.models import AttemptStatus, ShotSpec
from app.video.reliability.protocols import (
    CostBudgetProtocol,
    GovernorProtocol,
    QualityReputationProtocol,
    RouterProtocol,
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A ranked provider with the signals that produced its score."""

    provider: str
    score: float
    reputation: float
    load_factor: float
    est_cost_usd: float


@dataclass(frozen=True, slots=True)
class PrunedCandidate:
    """A provider that was excluded before ranking, with why (for the log)."""

    provider: str
    status: AttemptStatus
    detail: str


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    """The output of :func:`build_candidates`: who to try, in order, and who not to."""

    ranked: list[Candidate] = field(default_factory=list)
    pruned: list[PrunedCandidate] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.ranked


def _normalize_costs(costs: dict[str, float]) -> dict[str, float]:
    """Map raw USD estimates to [0,1] within the candidate set (max-relative).

    A single candidate (or all-zero costs) normalizes to 0.0 so cost doesn't tip
    the ranking when there's nothing to compare against.
    """
    if not costs:
        return {}
    hi = max(costs.values())
    if hi <= 0.0:
        return dict.fromkeys(costs, 0.0)
    return {p: c / hi for p, c in costs.items()}


def build_candidates(
    shot: ShotSpec,
    *,
    router: RouterProtocol,
    governor: GovernorProtocol,
    reputation: QualityReputationProtocol,
    budget: CostBudgetProtocol,
    config: ReliabilityConfig,
) -> CandidatePlan:
    """Produce the ranked, budget- and SLA-filtered candidate plan for ``shot``.

    Ordering of signals: governor admission first (a shed provider is never
    tried), then a budget pre-flight (estimated cost must fit remaining headroom),
    then a weighted rank of the survivors. The final list is truncated to
    ``config.max_providers``; everything truncated beyond the cap is *not* recorded
    as pruned (it simply wasn't needed) — only hard exclusions are.
    """
    providers = list(dict.fromkeys(router.candidates(shot)))  # de-dupe, keep order
    pruned: list[PrunedCandidate] = []
    remaining = budget.remaining_usd()

    admitted: list[str] = []
    est_costs: dict[str, float] = {}
    for provider in providers:
        if not governor.admit(provider, shot):
            pruned.append(
                PrunedCandidate(
                    provider=provider,
                    status=AttemptStatus.GOVERNOR_BLOCKED,
                    detail="governor did not admit (quota/SLA/load-shed)",
                )
            )
            continue
        est = max(0.0, budget.estimate(provider, shot))
        if est > remaining:
            pruned.append(
                PrunedCandidate(
                    provider=provider,
                    status=AttemptStatus.SKIPPED_NO_BUDGET_HEADROOM,
                    detail=f"est ${est:.4f} > remaining ${remaining:.4f}",
                )
            )
            continue
        admitted.append(provider)
        est_costs[provider] = est

    norm_cost = _normalize_costs(est_costs)
    scored: list[Candidate] = []
    for provider in admitted:
        rep = _clamp01(reputation.reputation(provider))
        load = _clamp01(governor.load_factor(provider))
        score = (
            config.weight_reputation * rep
            + config.weight_load_headroom * (1.0 - load)
            - config.weight_cost * norm_cost[provider]
        )
        scored.append(
            Candidate(
                provider=provider,
                score=score,
                reputation=rep,
                load_factor=load,
                est_cost_usd=est_costs[provider],
            )
        )

    # Stable, highest-score-first. ``sorted`` is stable, so equal scores preserve
    # the router's admission order (a deterministic tie-break).
    scored.sort(key=lambda c: c.score, reverse=True)
    ranked = scored[: config.max_providers]
    return CandidatePlan(ranked=ranked, pruned=pruned)


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


__all__ = ["Candidate", "CandidatePlan", "PrunedCandidate", "build_candidates"]
