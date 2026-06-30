"""Winner selection across scored candidates — pure, deterministic, tie-broken (§9.5).

The renderer fans a shot out and scores every candidate; this module turns that field
into one winner under a configurable objective. Every function here is pure (operates
on already-scored :class:`~app.video.ensemble.models.Candidate` s) so selection is
exhaustively unit-testable with no fakes at all.

Determinism is non-negotiable — two runs with identical scores must pick the same
winner — so every comparison resolves ties down a fixed cascade and the final
tie-break is the candidate's launch ``order`` (its position in the deterministic
fan-out), then its provider ``name``. There is no RNG and no wall-clock anywhere.

Objectives:

* ``MAX_QUALITY`` — highest composite (cost ignored).
* ``QUALITY_PER_COST`` — highest composite / cost (best value), with a quality margin
  so a trivially-better-but-pricier candidate doesn't beat a much cheaper near-equal.
* ``QUALITY_UNDER_COST_CAP`` — highest composite among candidates within the cap; a
  candidate over the cap is excluded entirely.
* ``CONSISTENCY_VOTE`` — most on-model (highest identity sub-score), composite as the
  first tie-break — for hero shots where locked-identity fidelity dominates.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Candidate, CostUnit, EnsembleConfig, Objective

#: A sort key over a candidate: a tuple compared lexicographically. Bigger is better,
#: so every "more is better" field is negated for an ascending ``sorted``. The trailing
#: ``(order, name)`` is the deterministic tie-break and is always present. Every element
#: is a float except the final provider-name string, so the tuple is order-comparable.
_SelectionKey = tuple[float | str, ...]


def eligible_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """The candidates that produced a usable score, in launch order."""
    return [c for c in candidates if c.is_eligible]


def within_cost_cap(candidate: Candidate, *, cap: float, unit: CostUnit) -> bool:
    """True when ``candidate`` is at or under the per-shot cost ``cap`` (0 → no cap)."""
    if cap <= 0:
        return True
    return candidate.cost_in(unit) <= cap + 1e-12


def _composite(candidate: Candidate) -> float:
    assert candidate.score is not None
    return candidate.score.composite


def _identity(candidate: Candidate) -> float:
    assert candidate.score is not None
    return candidate.score.identity


def _tiebreak(candidate: Candidate) -> tuple[int, str]:
    """The final deterministic tie-break: earliest launch order, then provider name."""
    return (candidate.order, candidate.provider)


def quality_per_cost(candidate: Candidate, *, unit: CostUnit) -> float:
    """Composite quality per unit of cost (higher = better value).

    A zero-cost candidate (e.g. a cache hit) is infinitely good value; we represent
    that as a large finite sentinel so the metric stays a plain float and still sorts
    above any positive-cost candidate, with composite then breaking the tie.
    """
    assert candidate.score is not None
    cost = candidate.cost_in(unit)
    if cost <= 0:
        return float("inf")
    return candidate.score.composite / cost


def is_good_enough(candidate: Candidate, threshold: float) -> bool:
    """True when this scored candidate clears the early-stop ``good_enough`` bar.

    A threshold of 0 or >1 disables early-stop (no candidate can clear it).
    """
    if threshold <= 0.0 or threshold > 1.0:
        return False
    return candidate.is_eligible and _composite(candidate) >= threshold


def _selection_key(candidate: Candidate, config: EnsembleConfig) -> _SelectionKey:
    """The objective's sort key (ascending; smaller tuple wins after negation)."""
    order, name_rank = _rank_name(candidate)
    if config.objective is Objective.MAX_QUALITY:
        return (-_composite(candidate), order, name_rank)
    if config.objective is Objective.QUALITY_PER_COST:
        # Primary: value (quality/cost). Tie-break: raw quality, then determinism.
        return (
            -quality_per_cost(candidate, unit=config.cost_unit),
            -_composite(candidate),
            order,
            name_rank,
        )
    if config.objective is Objective.QUALITY_UNDER_COST_CAP:
        # Within-cap candidates only reach here; rank by raw quality, then cheapest.
        return (
            -_composite(candidate),
            candidate.cost_in(config.cost_unit),
            order,
            name_rank,
        )
    if config.objective is Objective.CONSISTENCY_VOTE:
        # Most on-model first; composite breaks identity ties; then determinism.
        return (
            -_identity(candidate),
            -_composite(candidate),
            order,
            name_rank,
        )
    raise ValueError(f"unknown objective {config.objective!r}")  # pragma: no cover


def _rank_name(candidate: Candidate) -> tuple[float, str]:
    """Float-typed launch order + provider name for a homogeneous tuple key."""
    order, name = _tiebreak(candidate)
    return (float(order), name)


def selectable(candidates: Sequence[Candidate], config: EnsembleConfig) -> list[Candidate]:
    """The eligible candidates an objective may pick from (applies the cost cap).

    Only ``QUALITY_UNDER_COST_CAP`` *excludes* over-cap candidates from selection; the
    other objectives keep them (the cap is enforced at launch by the budget guard, so
    an already-rendered over-cap candidate is still a valid pick for, say, max-quality).
    """
    pool = eligible_candidates(candidates)
    if config.objective is Objective.QUALITY_UNDER_COST_CAP and config.per_shot_cost_cap > 0:
        pool = [
            c
            for c in pool
            if within_cost_cap(c, cap=config.per_shot_cost_cap, unit=config.cost_unit)
        ]
    return pool


def select_winner(candidates: Sequence[Candidate], config: EnsembleConfig) -> Candidate | None:
    """Pick the winning candidate per ``config.objective`` (deterministic), or ``None``.

    Returns ``None`` only when no eligible candidate survives the objective's filter
    (e.g. every candidate failed, or every one is over the cap under
    ``QUALITY_UNDER_COST_CAP``). The sort is stable and keyed by a fully-determined
    tuple, so the same scores always yield the same winner.
    """
    pool = selectable(candidates, config)
    if not pool:
        return None
    return min(pool, key=lambda c: _selection_key(c, config))


def explain_winner(
    winner: Candidate, candidates: Sequence[Candidate], config: EnsembleConfig
) -> str:
    """A short human-readable reason the winner beat the field (for the report)."""
    assert winner.score is not None
    pool = selectable(candidates, config)
    n = len(pool)
    obj = config.objective
    if obj is Objective.MAX_QUALITY:
        return f"highest composite quality {winner.score.composite:.3f} of {n} candidate(s)"
    if obj is Objective.QUALITY_PER_COST:
        value = quality_per_cost(winner, unit=config.cost_unit)
        cost = winner.cost_in(config.cost_unit)
        unit = "s" if config.cost_unit is CostUnit.VIDEO_SECONDS else "usd"
        return (
            f"best value: quality {winner.score.composite:.3f} / "
            f"{cost:.3f}{unit} = {value:.4f} of {n} candidate(s)"
        )
    if obj is Objective.QUALITY_UNDER_COST_CAP:
        cap = config.per_shot_cost_cap
        unit = "s" if config.cost_unit is CostUnit.VIDEO_SECONDS else "usd"
        return (
            f"highest composite {winner.score.composite:.3f} within "
            f"{cap:.3f}{unit} cap of {n} eligible candidate(s)"
        )
    if obj is Objective.CONSISTENCY_VOTE:
        return (
            f"most on-model: identity {winner.score.identity:.3f} "
            f"(composite {winner.score.composite:.3f}) of {n} candidate(s)"
        )
    return f"selected by {obj}"  # pragma: no cover


__all__ = [
    "eligible_candidates",
    "explain_winner",
    "is_good_enough",
    "quality_per_cost",
    "select_winner",
    "selectable",
    "within_cost_cap",
]
