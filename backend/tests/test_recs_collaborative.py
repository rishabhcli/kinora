"""Unit tests for the collaborative-filtering models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.recommendations.collaborative import (
    InteractionMatrix,
    ItemItemCF,
    UserUserCF,
)
from app.recommendations.types import Interaction, InteractionKind

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _ev(user: str, book: str, kind: InteractionKind = InteractionKind.LIKE) -> Interaction:
    return Interaction(user, book, kind, _NOW)


def test_matrix_accumulates_net_weight_both_orientations() -> None:
    events = [
        _ev("u1", "b1", InteractionKind.LIKE),
        _ev("u1", "b1", InteractionKind.VIEW),
        _ev("u1", "b2", InteractionKind.DISLIKE),
    ]
    m = InteractionMatrix.from_interactions(events)
    like = InteractionKind.LIKE.value  # noqa: F841 - readability
    expected_b1 = (
        InteractionMatrix.from_interactions([_ev("u1", "b1", InteractionKind.LIKE)]).user_items[
            "u1"
        ]["b1"]
        + InteractionMatrix.from_interactions([_ev("u1", "b1", InteractionKind.VIEW)]).user_items[
            "u1"
        ]["b1"]
    )
    assert m.user_items["u1"]["b1"] == pytest.approx(expected_b1)
    # Transpose is consistent.
    assert m.item_users["b1"]["u1"] == m.user_items["u1"]["b1"]
    # Net-negative b2 is excluded from engaged items.
    assert "b2" not in m.engaged_items("u1")
    assert "b1" in m.engaged_items("u1")


def test_item_item_recommends_cooccurring_book() -> None:
    # u1 & u2 both read b1 and b2; u1 also read b3. For u2, item-item should
    # surface b3 because b1/b2 co-occur with it via u1.
    events = [
        _ev("u1", "b1"),
        _ev("u1", "b2"),
        _ev("u1", "b3"),
        _ev("u2", "b1"),
        _ev("u2", "b2"),
    ]
    m = InteractionMatrix.from_interactions(events)
    cf = ItemItemCF(m, min_cooccur=1, neighbors=10)
    hits = cf.recommend("u2", k=5)
    ids = [h.book_id for h in hits]
    assert "b3" in ids
    # Already-engaged books are excluded.
    assert "b1" not in ids and "b2" not in ids
    # The hit is attributed to a seed book the user actually engaged with.
    b3 = next(h for h in hits if h.book_id == "b3")
    assert b3.seed_book_id in {"b1", "b2"}


def test_item_item_min_cooccur_suppresses_single_reader_edges() -> None:
    # b1 and b9 share only one reader (u1). With min_cooccur=2, no edge.
    events = [_ev("u1", "b1"), _ev("u1", "b9"), _ev("u2", "b1"), _ev("u2", "b2")]
    m = InteractionMatrix.from_interactions(events)
    strict = ItemItemCF(m, min_cooccur=2, neighbors=10)
    # b9 only co-occurs with b1 via the single reader u1 → filtered out.
    assert "b9" not in strict.similar_items("b1")


def test_user_user_recommends_via_neighbour() -> None:
    # u1 and u2 share b1,b2 (close neighbours); u2 also loves b5. Recommend b5 to u1.
    events = [
        _ev("u1", "b1"),
        _ev("u1", "b2"),
        _ev("u2", "b1"),
        _ev("u2", "b2"),
        _ev("u2", "b5"),
        # an unrelated user
        _ev("u3", "b8"),
    ]
    m = InteractionMatrix.from_interactions(events)
    cf = UserUserCF(m, neighbors=10)
    neighbours = [n for n, _ in cf.neighbours("u1")]
    assert neighbours and neighbours[0] == "u2"  # u2 is the nearest neighbour
    hits = cf.recommend("u1", k=5)
    ids = [h.book_id for h in hits]
    assert "b5" in ids
    assert "b1" not in ids  # already engaged


def test_cf_empty_for_unknown_user() -> None:
    m = InteractionMatrix.from_interactions([_ev("u1", "b1")])
    assert ItemItemCF(m).recommend("ghost", k=5) == []
    assert UserUserCF(m).recommend("ghost", k=5) == []
    assert UserUserCF(m).neighbours("ghost") == []
