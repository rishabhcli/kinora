"""Tests for the benchmark harness — metrics + ground truth correctness."""

from __future__ import annotations

import numpy as np
import pytest

from app.datascale.vectorsearch.benchmark import (
    average_precision,
    benchmark,
    build_brute_force,
    compare,
    exact_ground_truth,
    ndcg,
    recall_at_k,
)
from app.datascale.vectorsearch.brute_force import BruteForceIndex
from app.datascale.vectorsearch.builder import build_hnsw
from app.datascale.vectorsearch.types import Metric

from .conftest import Corpus


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert recall_at_k(["a", "x", "y"], ["a", "b", "c"]) == pytest.approx(1 / 3)
    assert recall_at_k([], ["a"]) == 0.0
    assert recall_at_k(["a"], []) == 1.0  # nothing relevant → trivially perfect


def test_average_precision_rewards_rank() -> None:
    truth = ["a", "b"]
    good = average_precision(["a", "b", "x"], truth)
    bad = average_precision(["x", "a", "b"], truth)
    assert good > bad
    assert average_precision(["a", "b"], truth) == pytest.approx(1.0)


def test_ndcg_perfect_and_degraded() -> None:
    truth = ["a", "b", "c"]
    assert ndcg(["a", "b", "c"], truth) == pytest.approx(1.0)
    assert ndcg(["x", "y", "z"], truth) == 0.0
    assert 0.0 < ndcg(["x", "a", "b"], truth) < 1.0


def test_exact_ground_truth_matches_brute_force(small_clustered: Corpus) -> None:
    truth = exact_ground_truth(
        small_clustered.ids, small_clustered.vectors, small_clustered.queries, 10
    )
    bf = BruteForceIndex(small_clustered.dim)
    bf.add_many(small_clustered.ids, small_clustered.rows())
    for qi in range(small_clustered.queries.shape[0]):
        assert truth[qi] == bf.exact_neighbors(small_clustered.queries[qi], 10)


def test_benchmark_brute_force_is_perfect(small_clustered: Corpus) -> None:
    bf = build_brute_force(small_clustered.ids, small_clustered.rows())
    report = benchmark(
        bf, small_clustered.ids, small_clustered.vectors, small_clustered.queries, k=10
    )
    assert report.recall_at_k == pytest.approx(1.0)
    assert report.map_at_k == pytest.approx(1.0)
    assert report.ndcg_at_k == pytest.approx(1.0)
    assert report.n_queries == small_clustered.queries.shape[0]
    assert report.qps > 0


def test_benchmark_report_as_dict(small_clustered: Corpus) -> None:
    bf = build_brute_force(small_clustered.ids, small_clustered.rows())
    report = benchmark(
        bf, small_clustered.ids, small_clustered.vectors, small_clustered.queries, k=5
    )
    d = report.as_dict()
    assert set(d) >= {"recall_at_k", "mean_latency_ms", "p95_latency_ms", "qps", "k"}
    assert d["k"] == 5


def test_compare_hnsw_vs_brute_force(small_clustered: Corpus) -> None:
    bf = build_brute_force(small_clustered.ids, small_clustered.rows())
    hnsw = build_hnsw(small_clustered.ids, small_clustered.rows(), dim=small_clustered.dim)
    reports = compare(
        {"brute": bf, "hnsw": hnsw},
        small_clustered.ids,
        small_clustered.vectors,
        small_clustered.queries,
        k=10,
        search_kwargs={"hnsw": {"ef": 120}},
    )
    assert reports["brute"].recall_at_k == pytest.approx(1.0)
    assert reports["hnsw"].recall_at_k >= 0.9
    # The approximate index should not beat exact recall.
    assert reports["hnsw"].recall_at_k <= reports["brute"].recall_at_k + 1e-9


def test_benchmark_accepts_numpy_arrays(small_clustered: Corpus) -> None:
    bf = build_brute_force(small_clustered.ids, small_clustered.rows())
    report = benchmark(
        bf,
        small_clustered.ids,
        np.asarray(small_clustered.vectors),
        np.asarray(small_clustered.queries),
        k=10,
    )
    assert report.recall_at_k == pytest.approx(1.0)


def test_benchmark_l2_metric(small_clustered: Corpus) -> None:
    bf = build_brute_force(small_clustered.ids, small_clustered.rows(), metric=Metric.L2)
    report = benchmark(
        bf,
        small_clustered.ids,
        small_clustered.vectors,
        small_clustered.queries,
        k=10,
        metric=Metric.L2,
    )
    assert report.recall_at_k == pytest.approx(1.0)
