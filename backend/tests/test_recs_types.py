"""Unit tests for the recommendations core types."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.recommendations.types import (
    DEFAULT_KIND_WEIGHTS,
    BlendWeights,
    BookFeatures,
    Candidate,
    Interaction,
    InteractionKind,
    Reason,
    ReasonKind,
    Recommendation,
    kind_weight,
)


def test_kind_weight_defaults_and_overrides() -> None:
    assert kind_weight(InteractionKind.LIKE) == DEFAULT_KIND_WEIGHTS[InteractionKind.LIKE]
    assert kind_weight(InteractionKind.DISLIKE) < 0.0
    overrides = {InteractionKind.VIEW: 9.0}
    assert kind_weight(InteractionKind.VIEW, overrides) == 9.0
    # An override for a different kind doesn't leak.
    assert (
        kind_weight(InteractionKind.LIKE, overrides) == DEFAULT_KIND_WEIGHTS[InteractionKind.LIKE]
    )


def test_interaction_signal_prefers_explicit_weight() -> None:
    now = datetime.now(UTC)
    explicit = Interaction("u", "b", InteractionKind.VIEW, now, weight=3.5)
    assert explicit.signal() == 3.5
    implicit = Interaction("u", "b", InteractionKind.FINISH, now)
    assert implicit.signal() == DEFAULT_KIND_WEIGHTS[InteractionKind.FINISH]


def test_book_features_has_embedding() -> None:
    assert not BookFeatures("b").has_embedding
    assert not BookFeatures("b", embedding=[0.0, 0.0]).has_embedding
    assert BookFeatures("b", embedding=[0.0, 1.0]).has_embedding


def test_candidate_merge_takes_max_per_signal_and_strongest_seed() -> None:
    a = Candidate(
        "b",
        source_scores={ReasonKind.CONTENT: 0.5},
        seeds={ReasonKind.CONTENT: ("seed_a", 0.5)},
    )
    b = Candidate(
        "b",
        source_scores={ReasonKind.CONTENT: 0.8, ReasonKind.TASTE: 0.3},
        seeds={ReasonKind.CONTENT: ("seed_b", 0.8)},
    )
    merged = a.merge(b)
    assert merged.source_scores[ReasonKind.CONTENT] == 0.8  # max wins
    assert merged.source_scores[ReasonKind.TASTE] == 0.3
    assert merged.seeds[ReasonKind.CONTENT] == ("seed_b", 0.8)  # strongest seed


def test_candidate_merge_rejects_mismatched_book() -> None:
    with pytest.raises(ValueError, match="different books"):
        Candidate("a").merge(Candidate("b"))


def test_blend_weights_reject_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        BlendWeights(content=-1.0)


def test_blend_weights_as_map() -> None:
    w = BlendWeights(content=1.0, collaborative=2.0, taste=3.0, popularity=0.5)
    m = w.as_map()
    assert m[ReasonKind.CONTENT] == 1.0
    assert m[ReasonKind.COLLABORATIVE] == 2.0
    assert m[ReasonKind.TASTE] == 3.0
    assert m[ReasonKind.POPULAR] == 0.5


def test_reason_and_recommendation_json_projection() -> None:
    reason = Reason(ReasonKind.CONTENT, 0.123456789, seed_book_id="b0", seed_title="X")
    d = reason.to_dict()
    assert d["kind"] == "content"
    assert d["contribution"] == round(0.123456789, 6)
    assert d["seed_title"] == "X"

    rec = Recommendation("b1", rank=1, score=0.9, title="T", reasons=(reason,), explanation="why")
    rd = rec.to_dict()
    assert rd["book_id"] == "b1"
    assert rd["rank"] == 1
    assert rd["explanation"] == "why"
    assert rd["reasons"][0]["kind"] == "content"
