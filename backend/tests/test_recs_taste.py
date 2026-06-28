"""Unit tests for the recency-decayed per-user taste model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.recommendations.taste import (
    TasteModel,
    decay_factor,
    engaged_book_weights,
    taste_similarity,
)
from app.recommendations.types import BookFeatures, Interaction, InteractionKind

_NOW = datetime(2026, 6, 1, tzinfo=UTC)
_DAY = 86_400.0


def test_decay_factor_half_life() -> None:
    assert decay_factor(0.0, 30.0) == pytest.approx(1.0)
    # Exactly one half-life → 0.5.
    assert decay_factor(30.0 * _DAY, 30.0) == pytest.approx(0.5)
    # Two half-lives → 0.25.
    assert decay_factor(60.0 * _DAY, 30.0) == pytest.approx(0.25)
    # Negative age clamps to weight 1.0.
    assert decay_factor(-100.0, 30.0) == pytest.approx(1.0)
    # Non-positive half-life disables decay.
    assert decay_factor(999.0 * _DAY, 0.0) == 1.0


def _feat(book_id: str, vec: list[float]) -> BookFeatures:
    return BookFeatures(book_id=book_id, embedding=vec)


def test_taste_vector_points_toward_engaged_books() -> None:
    features = {
        "fant": _feat("fant", [1.0, 0.0, 0.0]),
        "scifi": _feat("scifi", [0.0, 1.0, 0.0]),
    }
    events = [
        Interaction("u", "fant", InteractionKind.FINISH, _NOW),
        Interaction("u", "fant", InteractionKind.LIKE, _NOW),
        Interaction("u", "scifi", InteractionKind.VIEW, _NOW),
    ]
    acc = TasteModel(half_life_days=30.0).build(events, features, as_of=_NOW)
    vec = acc.vector()
    # Strongly engaged 'fant' axis dominates the weakly-viewed 'scifi' axis.
    assert vec[0] > vec[1] > 0.0
    assert acc.event_count == 3


def test_dislike_pulls_taste_away() -> None:
    features = {"x": _feat("x", [1.0, 0.0])}
    disliked = TasteModel(half_life_days=30.0).build(
        [Interaction("u", "x", InteractionKind.DISLIKE, _NOW)], features, as_of=_NOW
    )
    # The only signal is negative, so the summed vector points the other way.
    assert disliked.sum_vec[0] < 0.0


def test_recency_decay_weights_recent_more() -> None:
    features = {"old": _feat("old", [1.0, 0.0]), "new": _feat("new", [0.0, 1.0])}
    events = [
        Interaction("u", "old", InteractionKind.LIKE, _NOW - timedelta(days=60)),
        Interaction("u", "new", InteractionKind.LIKE, _NOW),
    ]
    vec = TasteModel(half_life_days=30.0).build(events, features, as_of=_NOW).vector()
    # 'new' (age 0, weight 1) outweighs 'old' (age 2 half-lives, weight 0.25).
    assert vec[1] > vec[0]


def test_fold_is_incremental_and_matches_full_rebuild() -> None:
    features = {"a": _feat("a", [1.0, 0.0]), "b": _feat("b", [0.0, 1.0])}
    batch1 = [Interaction("u", "a", InteractionKind.LIKE, _NOW - timedelta(days=10))]
    batch2 = [Interaction("u", "b", InteractionKind.LIKE, _NOW)]

    model = TasteModel(half_life_days=30.0)
    # Incremental: fold batch1 at its time, then batch2 at NOW.
    acc1 = model.build(batch1, features, as_of=_NOW - timedelta(days=10))
    acc2 = model.fold(acc1, batch2, features, as_of=_NOW)
    # Full rebuild at NOW with both batches.
    full = model.build(batch1 + batch2, features, as_of=_NOW)

    assert acc2.vector() == pytest.approx(full.vector(), abs=1e-9)
    assert acc2.event_count == full.event_count == 2


def test_cold_accumulator_has_empty_vector() -> None:
    acc = TasteModel().build([], {}, as_of=_NOW)
    assert acc.is_cold
    assert acc.vector() == []


def test_taste_model_rejects_nonpositive_half_life() -> None:
    with pytest.raises(ValueError, match="positive"):
        TasteModel(half_life_days=0.0)


def test_taste_similarity_zero_when_cold_or_no_embedding() -> None:
    assert taste_similarity([], _feat("a", [1.0])) == 0.0
    assert taste_similarity([1.0], _feat("a", [])) == 0.0
    assert taste_similarity([1.0, 0.0], _feat("a", [1.0, 0.0])) == pytest.approx(1.0)


def test_engaged_book_weights_drops_net_negative() -> None:
    features_unused = None  # noqa: F841 - signature documentation
    events = [
        Interaction("u", "loved", InteractionKind.LIKE, _NOW),
        Interaction("u", "hated", InteractionKind.DISLIKE, _NOW),
    ]
    weights = engaged_book_weights(events, as_of=_NOW, half_life_days=30.0)
    assert "loved" in weights
    assert "hated" not in weights  # net-negative dropped
