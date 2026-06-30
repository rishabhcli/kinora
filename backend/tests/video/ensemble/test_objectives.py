"""Pure selection-objective tests: best-by-objective, quality-per-cost, cost cap,
tie-break determinism, consistency vote — all over hand-built scored candidates."""

from __future__ import annotations

import pytest

from app.video.ensemble.models import (
    Candidate,
    CandidateStatus,
    CostUnit,
    EnsembleConfig,
    Objective,
    QualityScore,
    RenderOutput,
)
from app.video.ensemble.objectives import (
    eligible_candidates,
    is_good_enough,
    quality_per_cost,
    select_winner,
    selectable,
    within_cost_cap,
)


def _cand(
    name: str,
    *,
    order: int,
    composite: float,
    identity: float = 1.0,
    video_seconds: float = 5.0,
    usd: float = 0.0,
    status: CandidateStatus = CandidateStatus.SCORED,
) -> Candidate:
    score = (
        QualityScore(composite=composite, identity=identity)
        if status is CandidateStatus.SCORED
        else None
    )
    output = RenderOutput(model=name, duration_s=5.0) if status is CandidateStatus.SCORED else None
    return Candidate(
        provider=name,
        status=status,
        order=order,
        output=output,
        score=score,
        video_seconds=video_seconds if status is CandidateStatus.SCORED else 0.0,
        usd=usd if status is CandidateStatus.SCORED else 0.0,
    )


def _cfg(**kw: object) -> EnsembleConfig:
    base: dict[str, object] = {"enabled": True, "max_candidates": 3}
    base.update(kw)
    return EnsembleConfig(**base)


# --------------------------------------------------------------------------- #
# MAX_QUALITY
# --------------------------------------------------------------------------- #


def test_max_quality_picks_highest_composite() -> None:
    cands = [
        _cand("a", order=0, composite=0.70),
        _cand("b", order=1, composite=0.92),
        _cand("c", order=2, composite=0.81),
    ]
    winner = select_winner(cands, _cfg(objective=Objective.MAX_QUALITY))
    assert winner is not None
    assert winner.provider == "b"


def test_max_quality_ignores_cost() -> None:
    # The pricier candidate has the higher quality and must win under max-quality.
    cands = [
        _cand("cheap", order=0, composite=0.80, video_seconds=2.0),
        _cand("pricey", order=1, composite=0.95, video_seconds=20.0),
    ]
    winner = select_winner(cands, _cfg(objective=Objective.MAX_QUALITY))
    assert winner is not None and winner.provider == "pricey"


# --------------------------------------------------------------------------- #
# QUALITY_PER_COST
# --------------------------------------------------------------------------- #


def test_quality_per_cost_prefers_value() -> None:
    # b is slightly worse but a quarter of the cost → far better value.
    cands = [
        _cand("a", order=0, composite=0.96, video_seconds=12.0),  # 0.080 / s
        _cand("b", order=1, composite=0.90, video_seconds=3.0),  # 0.300 / s
    ]
    winner = select_winner(cands, _cfg(objective=Objective.QUALITY_PER_COST))
    assert winner is not None and winner.provider == "b"


def test_quality_per_cost_zero_cost_is_infinite_value() -> None:
    cands = [
        _cand("paid", order=0, composite=0.99, video_seconds=5.0),
        _cand("free", order=1, composite=0.60, video_seconds=0.0),
    ]
    assert quality_per_cost(cands[1], unit=CostUnit.VIDEO_SECONDS) == float("inf")
    winner = select_winner(cands, _cfg(objective=Objective.QUALITY_PER_COST))
    assert winner is not None and winner.provider == "free"


def test_quality_per_cost_uses_usd_when_configured() -> None:
    cands = [
        _cand("a", order=0, composite=0.90, video_seconds=5.0, usd=0.10),  # 9.0 / usd
        _cand("b", order=1, composite=0.80, video_seconds=5.0, usd=0.02),  # 40.0 / usd
    ]
    winner = select_winner(
        cands, _cfg(objective=Objective.QUALITY_PER_COST, cost_unit=CostUnit.USD)
    )
    assert winner is not None and winner.provider == "b"


# --------------------------------------------------------------------------- #
# QUALITY_UNDER_COST_CAP
# --------------------------------------------------------------------------- #


def test_cost_cap_excludes_over_cap_then_picks_best_under() -> None:
    cands = [
        _cand("best_but_pricey", order=0, composite=0.99, video_seconds=20.0),
        _cand("under_cap_top", order=1, composite=0.88, video_seconds=8.0),
        _cand("under_cap_low", order=2, composite=0.70, video_seconds=4.0),
    ]
    cfg = _cfg(objective=Objective.QUALITY_UNDER_COST_CAP, per_shot_cost_cap=10.0)
    pool = selectable(cands, cfg)
    assert {c.provider for c in pool} == {"under_cap_top", "under_cap_low"}
    winner = select_winner(cands, cfg)
    assert winner is not None and winner.provider == "under_cap_top"


def test_cost_cap_none_eligible_returns_none() -> None:
    cands = [_cand("a", order=0, composite=0.9, video_seconds=50.0)]
    cfg = _cfg(objective=Objective.QUALITY_UNDER_COST_CAP, per_shot_cost_cap=10.0)
    assert select_winner(cands, cfg) is None


def test_within_cost_cap_boundary_inclusive() -> None:
    c = _cand("a", order=0, composite=0.9, video_seconds=10.0)
    assert within_cost_cap(c, cap=10.0, unit=CostUnit.VIDEO_SECONDS) is True
    assert within_cost_cap(c, cap=9.99, unit=CostUnit.VIDEO_SECONDS) is False
    assert within_cost_cap(c, cap=0.0, unit=CostUnit.VIDEO_SECONDS) is True  # 0 → no cap


# --------------------------------------------------------------------------- #
# CONSISTENCY_VOTE
# --------------------------------------------------------------------------- #


def test_consistency_vote_picks_most_on_model() -> None:
    # c has lower composite but the best locked-identity fidelity.
    cands = [
        _cand("a", order=0, composite=0.95, identity=0.80),
        _cand("b", order=1, composite=0.93, identity=0.88),
        _cand("c", order=2, composite=0.85, identity=0.97),
    ]
    winner = select_winner(cands, _cfg(objective=Objective.CONSISTENCY_VOTE))
    assert winner is not None and winner.provider == "c"


def test_consistency_vote_composite_breaks_identity_tie() -> None:
    cands = [
        _cand("a", order=0, composite=0.80, identity=0.90),
        _cand("b", order=1, composite=0.92, identity=0.90),  # same identity, higher composite
    ]
    winner = select_winner(cands, _cfg(objective=Objective.CONSISTENCY_VOTE))
    assert winner is not None and winner.provider == "b"


# --------------------------------------------------------------------------- #
# Tie-break determinism
# --------------------------------------------------------------------------- #


def test_tiebreak_is_launch_order_then_name() -> None:
    # Three identical composites; earliest launch order must win, deterministically.
    cands = [
        _cand("z", order=2, composite=0.90),
        _cand("a", order=0, composite=0.90),
        _cand("m", order=1, composite=0.90),
    ]
    winner = select_winner(cands, _cfg(objective=Objective.MAX_QUALITY))
    assert winner is not None and winner.provider == "a"  # order 0


def test_tiebreak_repeatable_across_input_orderings() -> None:
    base = [
        _cand("a", order=0, composite=0.90),
        _cand("b", order=1, composite=0.90),
        _cand("c", order=2, composite=0.90),
    ]
    cfg = _cfg(objective=Objective.MAX_QUALITY)
    winners = {
        select_winner(perm, cfg).provider  # type: ignore[union-attr]
        for perm in (base, list(reversed(base)), [base[1], base[2], base[0]])
    }
    assert winners == {"a"}  # identical winner regardless of input order


def test_name_breaks_order_tie() -> None:
    # Same order (shouldn't happen in practice) → provider name decides.
    cands = [
        _cand("beta", order=0, composite=0.9),
        _cand("alpha", order=0, composite=0.9),
    ]
    winner = select_winner(cands, _cfg(objective=Objective.MAX_QUALITY))
    assert winner is not None and winner.provider == "alpha"


# --------------------------------------------------------------------------- #
# Eligibility + good-enough
# --------------------------------------------------------------------------- #


def test_failed_candidates_excluded_from_selection() -> None:
    cands = [
        _cand("ok", order=0, composite=0.40),
        _cand("dead", order=1, composite=0.0, status=CandidateStatus.FAILED),
    ]
    assert [c.provider for c in eligible_candidates(cands)] == ["ok"]
    winner = select_winner(cands, _cfg(objective=Objective.MAX_QUALITY))
    assert winner is not None and winner.provider == "ok"


def test_no_eligible_returns_none() -> None:
    cands = [_cand("dead", order=0, composite=0.0, status=CandidateStatus.FAILED)]
    assert select_winner(cands, _cfg(objective=Objective.MAX_QUALITY)) is None


@pytest.mark.parametrize(
    ("threshold", "composite", "expected"),
    [
        (0.85, 0.90, True),
        (0.85, 0.85, True),  # boundary inclusive
        (0.85, 0.84, False),
        (0.0, 0.99, False),  # 0 disables early-stop
        (1.5, 0.99, False),  # >1 disables early-stop
    ],
)
def test_is_good_enough(threshold: float, composite: float, expected: bool) -> None:
    c = _cand("a", order=0, composite=composite)
    assert is_good_enough(c, threshold) is expected
