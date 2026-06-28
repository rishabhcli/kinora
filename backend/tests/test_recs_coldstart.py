"""Unit tests for cold-start popularity + content fallback."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from app.recommendations.coldstart import (
    PopularityModel,
    damp_popularity,
    is_cold_user,
    popularity_candidates,
)
from app.recommendations.types import BookFeatures, Interaction, InteractionKind

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_damp_popularity_monotone_and_bounded() -> None:
    assert damp_popularity(0.0, damping=10.0) == 0.0
    a = damp_popularity(5.0, damping=10.0)
    b = damp_popularity(50.0, damping=10.0)
    assert 0.0 < a < b < 1.0  # monotone increasing, saturating below 1
    # Zero damping → a hard 1.0 for any positive mass.
    assert damp_popularity(0.1, damping=0.0) == 1.0


def test_popularity_from_interactions_decays_and_orders() -> None:
    events = [
        # Hot recent book.
        Interaction("u1", "hot", InteractionKind.LIKE, _NOW),
        Interaction("u2", "hot", InteractionKind.FINISH, _NOW),
        # Once-popular but stale book.
        Interaction("u3", "stale", InteractionKind.LIKE, _NOW - timedelta(days=120)),
        Interaction("u4", "stale", InteractionKind.LIKE, _NOW - timedelta(days=120)),
        # A dislike contributes nothing to popularity.
        Interaction("u5", "dud", InteractionKind.DISLIKE, _NOW),
    ]
    model = PopularityModel.from_interactions(events, as_of=_NOW, half_life_days=30.0)
    assert model.score("hot") > model.score("stale")  # recency wins
    assert model.raw("dud") == 0.0  # dislike adds no popularity
    top = model.top(k=2)
    assert [b for b, _ in top] == ["hot", "stale"]


def test_popularity_feature_prior_seeds_unseen_books() -> None:
    feats = {"primed": BookFeatures("primed", popularity=12.0)}
    model = PopularityModel.from_interactions([], as_of=_NOW, feature_prior=feats)
    assert model.raw("primed") == 12.0
    assert model.score("primed") > 0.0


def test_popularity_top_respects_universe_and_exclude() -> None:
    events = [
        Interaction("u", "a", InteractionKind.LIKE, _NOW),
        Interaction("u", "b", InteractionKind.LIKE, _NOW),
    ]
    model = PopularityModel.from_interactions(events, as_of=_NOW)
    out = model.top(k=5, exclude={"a"}, universe={"a", "b", "c"})
    ids = [b for b, _ in out]
    assert "a" not in ids  # excluded
    assert "b" in ids
    assert "c" not in ids  # zero popularity, filtered


def test_is_cold_user() -> None:
    assert is_cold_user(0)
    assert is_cold_user(1)  # below default min_for_cf=2
    assert not is_cold_user(2)
    assert not is_cold_user(5)


def test_popularity_candidates_helper() -> None:
    events = [Interaction("u", "a", InteractionKind.LIKE, _NOW)]
    model = PopularityModel.from_interactions(events, as_of=_NOW)
    out = popularity_candidates(model, k=5)
    assert out and out[0][0] == "a"
    assert math.isfinite(out[0][1])


def test_popularity_empty_log_yields_nothing() -> None:
    model = PopularityModel.from_interactions([], as_of=_NOW)
    assert model.top(k=5) == []
    assert model.score("anything") == 0.0
