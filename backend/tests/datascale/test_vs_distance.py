"""Unit tests for the distance kernels and core types (pure, no infra)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.datascale.vectorsearch import distance as dist
from app.datascale.vectorsearch.types import (
    FLOAT,
    Metric,
    SearchResult,
    as_matrix,
    as_vector,
)


def test_metric_ordering_semantics() -> None:
    assert Metric.COSINE.is_similarity and not Metric.COSINE.is_distance
    assert Metric.DOT.is_similarity
    assert Metric.L2.is_distance and not Metric.L2.is_similarity
    assert Metric.L2SQ.is_distance


def test_metric_is_str_enum() -> None:
    assert str(Metric.COSINE) == "cosine"
    assert Metric("l2") is Metric.L2
    assert dist.metric_of("DOT") is Metric.DOT


def test_metric_of_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        dist.metric_of("manhattan")


def test_normalize_unit_length() -> None:
    v = np.array([3.0, 4.0], dtype=FLOAT)
    n = dist.normalize(v)
    assert math.isclose(float(np.linalg.norm(n)), 1.0, rel_tol=1e-6)


def test_normalize_zero_vector_is_unchanged() -> None:
    z = np.zeros(5, dtype=FLOAT)
    assert np.allclose(dist.normalize(z), z)


def test_normalize_matrix_rows_unit() -> None:
    rng = np.random.default_rng(0)
    m = rng.standard_normal((10, 8)).astype(FLOAT)
    n = dist.normalize_matrix(m)
    assert np.allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-6)


def test_cosine_via_normalized_dot_matches_definition() -> None:
    rng = np.random.default_rng(1)
    a = dist.normalize(rng.standard_normal(16).astype(FLOAT))
    b = dist.normalize(rng.standard_normal(16).astype(FLOAT))
    raw = float(np.dot(a, b))
    assert math.isclose(dist.native_score(a, b, Metric.COSINE), raw, rel_tol=1e-6)


def test_order_value_negates_similarity() -> None:
    a = np.array([1.0, 0.0], dtype=FLOAT)
    b = np.array([1.0, 0.0], dtype=FLOAT)
    # identical → cosine 1.0 → order key -1.0 (smaller is closer)
    assert math.isclose(dist.order_value(a, b, Metric.COSINE), -1.0, rel_tol=1e-6)
    # L2 distance 0 → order key 0
    assert math.isclose(dist.order_value(a, b, Metric.L2), 0.0, abs_tol=1e-6)


def test_score_order_round_trip() -> None:
    for metric in Metric:
        s = 0.37
        o = dist.score_to_order(s, metric)
        assert math.isclose(dist.order_to_score(o, metric), s, rel_tol=1e-9)


def test_batch_matches_scalar() -> None:
    rng = np.random.default_rng(2)
    q = rng.standard_normal(12).astype(FLOAT)
    mat = rng.standard_normal((20, 12)).astype(FLOAT)
    for metric in Metric:
        batch = dist.order_value_batch(q, mat, metric)
        scalar = np.array([dist.order_value(q, mat[i], metric) for i in range(20)])
        assert np.allclose(batch, scalar, atol=1e-4)


def test_pairwise_l2_matches_loop() -> None:
    rng = np.random.default_rng(3)
    qs = rng.standard_normal((5, 10)).astype(FLOAT)
    mat = rng.standard_normal((8, 10)).astype(FLOAT)
    pw = dist.pairwise_order(qs, mat, Metric.L2)
    for i in range(5):
        for j in range(8):
            d = float(np.linalg.norm(qs[i] - mat[j]))
            assert math.isclose(pw[i, j], d, rel_tol=1e-3, abs_tol=1e-3)


def test_pairwise_cosine_orders_like_negative_similarity() -> None:
    rng = np.random.default_rng(4)
    qs = dist.normalize_matrix(rng.standard_normal((3, 6)).astype(FLOAT))
    mat = dist.normalize_matrix(rng.standard_normal((7, 6)).astype(FLOAT))
    pw = dist.pairwise_order(qs, mat, Metric.COSINE)
    sims = qs @ mat.T
    assert np.allclose(pw, -sims, atol=1e-5)


def test_as_vector_validates_shape_and_finiteness() -> None:
    assert as_vector([1.0, 2.0, 3.0]).dtype == FLOAT
    with pytest.raises(ValueError):
        as_vector([[1.0, 2.0]])  # type: ignore[list-item]  # 2-D rejected
    with pytest.raises(ValueError):
        as_vector([1.0, float("nan")])
    with pytest.raises(ValueError):
        as_vector([1.0, 2.0], dim=3)


def test_as_matrix_empty_and_mismatch() -> None:
    assert as_matrix([], dim=4).shape == (0, 4)
    with pytest.raises(ValueError):
        as_matrix([[1.0, 2.0], [1.0, 2.0, 3.0]])


def test_random_unit_vectors_deterministic_and_unit() -> None:
    a = dist.random_unit_vectors(5, 7, seed=99)
    b = dist.random_unit_vectors(5, 7, seed=99)
    assert np.array_equal(a, b)
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0, atol=1e-6)


def test_search_result_orders_closest_first() -> None:
    a = SearchResult(id="a", distance=0.1, score=0.9)
    b = SearchResult(id="b", distance=0.5, score=0.5)
    assert sorted([b, a]) == [a, b]
