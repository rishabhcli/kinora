"""Unit tests for the blended scorer + MMR diversity re-rank + boosts + reasons."""

from __future__ import annotations

import pytest

from app.recommendations import reasons as reason_synth
from app.recommendations.ranker import (
    BoostContext,
    apply_boosts,
    blend_candidate,
    default_boost_rules,
    diversify,
    same_author_boost,
    score_candidates,
    shared_tag_boost,
    to_recommendations,
)
from app.recommendations.types import (
    BlendWeights,
    BookFeatures,
    Candidate,
    Reason,
    ReasonKind,
    ScoredCandidate,
)


def test_blend_candidate_linear_combination() -> None:
    weights = BlendWeights(content=2.0, collaborative=1.0, taste=0.0, popularity=0.5)
    cand = Candidate(
        "b",
        source_scores={
            ReasonKind.CONTENT: 0.5,
            ReasonKind.COLLABORATIVE: 1.0,
            ReasonKind.POPULAR: 0.4,
            ReasonKind.TASTE: 0.9,  # weight 0 → no contribution
        },
        seeds={ReasonKind.CONTENT: ("seed", 0.5)},
    )
    scored = blend_candidate(cand, weights)
    # 2*0.5 + 1*1.0 + 0.5*0.4 + 0*0.9 = 2.2
    assert scored.score == pytest.approx(2.2)
    kinds = {r.kind for r in scored.reasons}
    assert ReasonKind.TASTE not in kinds  # zero contribution dropped
    assert ReasonKind.CONTENT in kinds


def test_blend_candidate_attaches_seed_title() -> None:
    feats = {"seed": BookFeatures("seed", title="The Seed")}
    cand = Candidate(
        "b",
        source_scores={ReasonKind.CONTENT: 1.0},
        seeds={ReasonKind.CONTENT: ("seed", 1.0)},
    )
    scored = blend_candidate(cand, BlendWeights(), features=feats)
    content_reason = next(r for r in scored.reasons if r.kind is ReasonKind.CONTENT)
    assert content_reason.seed_book_id == "seed"
    assert content_reason.seed_title == "The Seed"


def test_apply_boosts_adds_and_records_reason() -> None:
    feats = {
        "b": BookFeatures("b", author="Tolkien", tags=("fantasy",)),
        "loved": BookFeatures("loved", author="Tolkien", tags=("fantasy",)),
    }
    scored = ScoredCandidate("b", score=1.0, reasons=())
    ctx = BoostContext(
        book=feats["b"],
        features=feats,
        engaged_book_ids=frozenset({"loved"}),
        loved_book_ids=frozenset({"loved"}),
    )
    boosted = apply_boosts(scored, [same_author_boost(0.2), shared_tag_boost(0.1)], ctx)
    assert boosted.score == pytest.approx(1.3)
    assert any(r.kind is ReasonKind.BOOST for r in boosted.reasons)


def test_boost_rules_abstain_when_no_affinity() -> None:
    feats = {"b": BookFeatures("b", author="Nobody", tags=("scifi",))}
    ctx = BoostContext(
        book=feats["b"],
        features=feats,
        engaged_book_ids=frozenset(),
        loved_book_ids=frozenset(),
    )
    scored = ScoredCandidate("b", score=1.0)
    out = apply_boosts(scored, default_boost_rules(), ctx)
    assert out.score == 1.0  # nothing fired


def test_score_candidates_sorted_descending() -> None:
    weights = BlendWeights()
    cands = [
        Candidate("low", source_scores={ReasonKind.CONTENT: 0.1}),
        Candidate("high", source_scores={ReasonKind.CONTENT: 0.9}),
        Candidate("mid", source_scores={ReasonKind.CONTENT: 0.5}),
    ]
    out = score_candidates(cands, weights)
    assert [s.book_id for s in out] == ["high", "mid", "low"]


def test_diversify_picks_relevant_then_diverse() -> None:
    # Two near-identical high-scorers + one slightly-lower but orthogonal book.
    a = ScoredCandidate("a", score=1.0, vector=[1.0, 0.0])
    a2 = ScoredCandidate("a2", score=0.98, vector=[0.99, 0.01])
    b = ScoredCandidate("b", score=0.8, vector=[0.0, 1.0])
    out = diversify([a, a2, b], k=2, mmr_lambda=0.5)
    ids = [s.book_id for s in out]
    # First pick is the top scorer; MMR then prefers the orthogonal 'b' over the
    # near-duplicate 'a2'.
    assert ids[0] == "a"
    assert ids[1] == "b"


def test_diversify_appends_vectorless_candidates() -> None:
    with_vec = ScoredCandidate("v", score=1.0, vector=[1.0, 0.0])
    no_vec = ScoredCandidate("n", score=0.9, vector=[])
    out = diversify([with_vec, no_vec], k=5)
    ids = {s.book_id for s in out}
    assert ids == {"v", "n"}


def test_to_recommendations_ranks_and_explains() -> None:
    feats = {"b": BookFeatures("b", title="Title", author="Auth")}
    scored = [
        ScoredCandidate(
            "b",
            score=1.0,
            reasons=(Reason(ReasonKind.CONTENT, 1.0, seed_book_id="s", seed_title="Seed"),),
        )
    ]
    recs = to_recommendations(scored, features=feats)
    assert recs[0].rank == 1
    assert recs[0].title == "Title"
    assert recs[0].explanation == "Because you read Seed"


def test_reason_synthesis_phrases() -> None:
    assert reason_synth.explain(()) == "Recommended for you"
    coll = (Reason(ReasonKind.COLLABORATIVE, 1.0, seed_title="X"),)
    assert "also watched" in reason_synth.explain(coll)
    taste = (Reason(ReasonKind.TASTE, 1.0),)
    assert reason_synth.explain(taste) == "Matches your reading taste"
    pop = (Reason(ReasonKind.POPULAR, 1.0),)
    assert reason_synth.explain(pop) == "Trending on Kinora"


def test_summarize_dedups_and_limits() -> None:
    reasons = (
        Reason(ReasonKind.CONTENT, 2.0, seed_title="A"),
        Reason(ReasonKind.TASTE, 1.0),
        Reason(ReasonKind.POPULAR, 0.5),
    )
    out = reason_synth.summarize(reasons, limit=2)
    assert len(out) == 2
    assert out[0] == "Because you read A"  # highest contribution first
