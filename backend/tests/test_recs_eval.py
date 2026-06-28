"""Tests for the offline eval harness metrics + the engine-beats-baseline proof."""

from __future__ import annotations

import math

import pytest

from app.recommendations import eval as ev
from app.recommendations.engine import RecommendationEngine
from app.recommendations.synthetic import cluster_self_similarity, make_dataset
from app.recommendations.types import BookFeatures, RecsConfig

# --------------------------------------------------------------------------- #
# Pure ranking metrics — pinned against hand-computed values
# --------------------------------------------------------------------------- #


def test_precision_recall_at_k() -> None:
    ranked = ["a", "b", "c", "d"]
    relevant = {"a", "c", "z"}
    # 2 of top-4 relevant.
    assert ev.precision_at_k(ranked, relevant, 4) == pytest.approx(0.5)
    # 2 of 3 relevant recovered.
    assert ev.recall_at_k(ranked, relevant, 4) == pytest.approx(2 / 3)
    # Empty relevant set → 0 recall.
    assert ev.recall_at_k(ranked, set(), 4) == 0.0


def test_average_precision() -> None:
    # relevant at ranks 1 and 3 → AP = (1/1 + 2/3) / min(2, k)
    ranked = ["a", "b", "c"]
    relevant = {"a", "c"}
    expected = (1.0 + 2.0 / 3.0) / 2.0
    assert ev.average_precision(ranked, relevant, 3) == pytest.approx(expected)


def test_ndcg_perfect_and_imperfect() -> None:
    relevant = {"a", "b"}
    # Perfect ranking → nDCG 1.0.
    assert ev.ndcg_at_k(["a", "b", "c"], relevant, 3) == pytest.approx(1.0)
    # Relevant items pushed down → DCG = 1/log2(3) + 1/log2(4); IDCG = 1 + 1/log2(3).
    dcg = 1.0 / math.log2(3) + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert ev.ndcg_at_k(["c", "a", "b"], relevant, 3) == pytest.approx(dcg / idcg)


def test_reciprocal_rank() -> None:
    assert ev.reciprocal_rank(["x", "a"], {"a"}, 5) == pytest.approx(0.5)
    assert ev.reciprocal_rank(["x", "y"], {"a"}, 5) == 0.0


def test_intra_list_diversity() -> None:
    feats = {
        "a": BookFeatures("a", embedding=[1.0, 0.0]),
        "b": BookFeatures("b", embedding=[0.0, 1.0]),  # orthogonal
        "c": BookFeatures("c", embedding=[1.0, 0.0]),  # identical to a
    }
    # a vs b dissimilarity 1.0; a vs c 0.0; b vs c 1.0 → mean 2/3.
    assert ev.intra_list_diversity(["a", "b", "c"], feats) == pytest.approx(2 / 3)
    # Single item → 0.
    assert ev.intra_list_diversity(["a"], feats) == 0.0


def test_novelty_rewards_rare_items() -> None:
    shares = {"common": 0.5, "rare": 0.01}
    n_common = ev.novelty(["common"], shares)
    n_rare = ev.novelty(["rare"], shares)
    assert n_rare > n_common  # rarer → higher self-information
    assert n_common == pytest.approx(-math.log2(0.5))


def test_popularity_shares_normalize() -> None:
    shares = ev.popularity_shares({"a": 3.0, "b": 1.0})
    assert shares["a"] == pytest.approx(0.75)
    assert shares["b"] == pytest.approx(0.25)
    assert ev.popularity_shares({}) == {}


# --------------------------------------------------------------------------- #
# Synthetic dataset + the engine-beats-baselines proof
# --------------------------------------------------------------------------- #


def test_synthetic_dataset_is_separable_and_reproducible() -> None:
    ds1 = make_dataset(seed=7)
    ds2 = make_dataset(seed=7)
    # Reproducible: same seed → same interaction count + held-out labels.
    assert len(ds1.interactions) == len(ds2.interactions)
    assert ds1.held_out == ds2.held_out
    # The clusters are genuinely separable (content has signal to recover).
    assert cluster_self_similarity(ds1) > 0.3
    # Held-out positives are excluded from the training split.
    training = ds1.training_interactions()
    held_pairs = {(u, b) for u, books in ds1.held_out.items() for b in books}
    assert not any((e.user_id, e.book_id) in held_pairs for e in training)


def _build_harness(ds: object) -> tuple[ev.EvalHarness, dict[str, float], dict[str, set[str]]]:
    training = ds.training_interactions()  # type: ignore[attr-defined]
    counts = ev.book_engagement_counts(training)
    shares = ev.popularity_shares(counts)
    engaged_by_user: dict[str, set[str]] = {}
    for e in training:
        engaged_by_user.setdefault(e.user_id, set()).add(e.book_id)
    harness = ev.EvalHarness(
        ground_truth=ds.held_out,  # type: ignore[attr-defined]
        features=ds.features,  # type: ignore[attr-defined]
        popularity_share=shares,
        corpus_size=len(ds.features),  # type: ignore[attr-defined]
    )
    return harness, counts, engaged_by_user


def test_engine_beats_popularity_and_random_baselines() -> None:
    ds = make_dataset(seed=11, num_books=80, num_users=40)
    harness, counts, engaged_by_user = _build_harness(ds)

    engine = RecommendationEngine(RecsConfig(candidates_per_source=50, top_k=10))
    engine_rep = harness.evaluate(ev.EngineRecommender(engine, ds), k=10)
    pop_rep = harness.evaluate(ev.PopularityRecommender(counts, engaged_by_user), k=10)
    rnd_rep = harness.evaluate(ev.RandomRecommender(list(ds.features), seed=3), k=10)

    # The personalized engine should dominate the un-personalized baselines on
    # the rank-sensitive accuracy metrics — the headline recsys claim.
    assert engine_rep.ndcg > pop_rep.ndcg
    assert engine_rep.ndcg > rnd_rep.ndcg
    assert engine_rep.recall > pop_rep.recall
    assert engine_rep.map > pop_rep.map
    # And it should be genuinely good in absolute terms, not just relatively.
    assert engine_rep.hit_rate > 0.7
    assert engine_rep.recall > 0.4
    # Coverage: personalization spreads recommendations across the catalog far
    # more than the head-only popularity baseline.
    assert engine_rep.coverage > pop_rep.coverage


def test_eval_report_json_projection() -> None:
    ds = make_dataset(seed=5, num_books=40, num_users=20)
    harness, _, _ = _build_harness(ds)
    engine = RecommendationEngine(RecsConfig(top_k=5))
    rep = harness.evaluate(ev.EngineRecommender(engine, ds), k=5)
    d = rep.to_dict()
    assert d["k"] == 5
    assert set(d) >= {"precision", "recall", "ndcg", "map", "coverage", "diversity", "novelty"}
    assert 0.0 <= d["coverage"] <= 1.0
