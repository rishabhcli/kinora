"""End-to-end tests for the recommendation engine pipeline (pure, no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.recommendations.engine import RecommendationEngine, make_config_from_settings
from app.recommendations.types import (
    BookFeatures,
    Interaction,
    InteractionKind,
    ReasonKind,
    RecsConfig,
)

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _corpus() -> dict[str, BookFeatures]:
    return {
        "fant1": BookFeatures(
            "fant1",
            "Snow Queen",
            author="Andersen",
            embedding=[1.0, 0.0, 0.0],
            tags=("fantasy",),
            popularity=10.0,
        ),
        "fant2": BookFeatures(
            "fant2",
            "Ice King",
            author="Andersen",
            embedding=[0.95, 0.05, 0.0],
            tags=("fantasy",),
            popularity=5.0,
        ),
        "scifi1": BookFeatures(
            "scifi1", "Star Drive", embedding=[0.0, 1.0, 0.0], tags=("scifi",), popularity=8.0
        ),
        "scifi2": BookFeatures(
            "scifi2", "Nebula", embedding=[0.0, 0.95, 0.05], tags=("scifi",), popularity=3.0
        ),
        "rom1": BookFeatures(
            "rom1", "Love Letters", embedding=[0.0, 0.0, 1.0], tags=("romance",), popularity=20.0
        ),
    }


def test_engine_recommends_content_similar_books() -> None:
    feats = _corpus()
    events = [Interaction("u", "fant1", InteractionKind.FINISH, _NOW)]
    recs = RecommendationEngine(RecsConfig(top_k=5)).recommend(
        "u", interactions=events, features=feats, as_of=_NOW
    )
    ids = [r.book_id for r in recs]
    # The fantasy sibling should outrank the unrelated genres.
    assert "fant2" in ids
    assert ids.index("fant2") < ids.index("rom1")
    # The engaged book itself is never recommended back.
    assert "fant1" not in ids


def test_engine_collaborative_surfaces_neighbour_books() -> None:
    feats = _corpus()
    events = [
        # target user
        Interaction("u", "fant1", InteractionKind.FINISH, _NOW),
        # neighbour who also read fant1 + fant2
        Interaction("v", "fant1", InteractionKind.FINISH, _NOW),
        Interaction("v", "fant2", InteractionKind.LIKE, _NOW),
    ]
    recs = RecommendationEngine(RecsConfig(top_k=5)).recommend(
        "u", interactions=events, features=feats, as_of=_NOW
    )
    fant2 = next(r for r in recs if r.book_id == "fant2")
    kinds = {reason.kind for reason in fant2.reasons}
    # fant2 should now carry BOTH a content and a collaborative reason.
    assert ReasonKind.COLLABORATIVE in kinds
    assert ReasonKind.CONTENT in kinds


def test_engine_cold_user_falls_back_to_popularity() -> None:
    feats = _corpus()
    recs = RecommendationEngine(RecsConfig(top_k=3)).recommend(
        "new_user", interactions=[], features=feats, as_of=_NOW
    )
    assert recs  # never empty — popularity floor
    # The most popular book should head the cold-start list.
    assert recs[0].book_id == "rom1"
    assert all(r.reasons[0].kind is ReasonKind.POPULAR for r in recs)


def test_engine_respects_top_k() -> None:
    feats = _corpus()
    events = [Interaction("u", "fant1", InteractionKind.FINISH, _NOW)]
    recs = RecommendationEngine(RecsConfig(top_k=2)).recommend(
        "u", interactions=events, features=feats, as_of=_NOW, top_k=2
    )
    assert len(recs) == 2
    assert [r.rank for r in recs] == [1, 2]


def test_engine_dislike_does_not_recommend_disliked_book() -> None:
    feats = _corpus()
    events = [
        Interaction("u", "fant1", InteractionKind.FINISH, _NOW),
        Interaction("u", "scifi1", InteractionKind.DISLIKE, _NOW),
    ]
    recs = RecommendationEngine(RecsConfig(top_k=5)).recommend(
        "u", interactions=events, features=feats, as_of=_NOW
    )
    ids = [r.book_id for r in recs]
    assert "scifi1" not in ids  # interacted-with, never recommended back


def test_make_config_from_settings_reads_overrides() -> None:
    class FakeSettings:
        recs_weight_taste = 5.0
        recs_top_k = 7
        recs_mmr_lambda = 0.4

    cfg = make_config_from_settings(FakeSettings())
    assert cfg.weights.taste == 5.0
    assert cfg.top_k == 7
    assert cfg.mmr_lambda == 0.4
    # Unspecified knobs fall back to defaults.
    assert cfg.weights.content == 1.0


def test_make_config_from_bare_object_uses_defaults() -> None:
    cfg = make_config_from_settings(object())
    assert cfg.top_k == RecsConfig().top_k
    assert cfg.weights.taste == RecsConfig().weights.taste


def test_engine_recency_decay_changes_recommendations() -> None:
    feats = _corpus()
    # A user whose fantasy interest is ancient and scifi interest is fresh.
    events = [
        Interaction("u", "fant1", InteractionKind.LIKE, _NOW - timedelta(days=180)),
        Interaction("u", "scifi1", InteractionKind.LIKE, _NOW),
    ]
    recs = RecommendationEngine(RecsConfig(top_k=5, taste_half_life_days=15.0)).recommend(
        "u", interactions=events, features=feats, as_of=_NOW
    )
    ids = [r.book_id for r in recs]
    # Fresh scifi taste should rank scifi2 above the stale-fantasy fant2.
    assert ids.index("scifi2") < ids.index("fant2")
