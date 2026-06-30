"""Compare planned backends — the router-facing selection helper (pure logic).

Given one :class:`~app.video.planning.plan.CanonicalVideoRequest` and several
candidate :class:`~app.video.planning.capabilities.CapabilityProfile` s, plan each
and rank them so a router can pick the backend that renders the request with the
least fidelity loss (and, on ties, the fewest segments and the most-preferred
listed order). Infeasible plans sort last (``inf`` cost) but are still returned.

This is deliberately separate from :func:`app.video.planning.planner.plan` so the
single-backend planner stays focused; nothing here is network-aware.
"""

from __future__ import annotations

from collections.abc import Sequence

from .capabilities import CapabilityProfile
from .plan import CanonicalVideoRequest, FidelityPenalty, RenderPlan
from .planner import plan as plan_one


def plan_all(
    request: CanonicalVideoRequest,
    profiles: Sequence[CapabilityProfile],
    *,
    weights: dict[FidelityPenalty, float] | None = None,
) -> list[RenderPlan]:
    """Plan ``request`` against every profile, preserving input order."""
    return [plan_one(request, p, weights=weights) for p in profiles]


def rank_plans(plans: Sequence[RenderPlan]) -> list[RenderPlan]:
    """Order plans best-first by (feasible, fidelity_cost, segments, input order).

    A stable sort over ``enumerate`` keeps the caller's preference order as the
    final tie-break, so equal-cost feasible backends fall back to declared
    priority. Infeasible plans (cost ``inf``) sort to the end.
    """
    indexed = list(enumerate(plans))
    return [
        p
        for _, p in sorted(
            indexed,
            key=lambda iv: (
                0 if iv[1].feasible else 1,
                iv[1].fidelity_cost,
                iv[1].segment_count,
                iv[0],
            ),
        )
    ]


def best_plan(
    request: CanonicalVideoRequest,
    profiles: Sequence[CapabilityProfile],
    *,
    weights: dict[FidelityPenalty, float] | None = None,
) -> RenderPlan:
    """The single most-faithful plan across ``profiles``.

    Raises:
        ValueError: when ``profiles`` is empty.
    """
    if not profiles:
        raise ValueError("best_plan requires at least one capability profile")
    return rank_plans(plan_all(request, profiles, weights=weights))[0]


__all__ = ["best_plan", "plan_all", "rank_plans"]
