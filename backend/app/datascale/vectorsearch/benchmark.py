"""Recall / latency benchmarking against a brute-force ground truth.

The only honest way to know an approximate index is good enough is to measure
its overlap with the *exact* answer. This module computes the exact top-``k`` for
a query set with :class:`~app.datascale.vectorsearch.brute_force.BruteForceIndex`,
then scores any index that exposes a ``search(vector, k, ...) -> [SearchResult]``
method on:

* **recall@k** — mean fraction of the exact top-``k`` the index returns.
* **mean average precision (mAP@k)** and **nDCG@k** — rank-sensitive quality.
* **latency** — per-query wall time (mean / p50 / p95 / p99) and QPS.

All deterministic given a seed; no infra. The returned :class:`BenchmarkReport`
is a plain dataclass so tests can assert ``recall@k >= threshold`` directly.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from . import distance as dist
from .brute_force import BruteForceIndex
from .types import FLOAT, Metric, SearchResult, VectorId, as_matrix


@runtime_checkable
class Searchable(Protocol):
    """The minimal surface a benchmark target must expose.

    Concrete indexes (HNSW, sharded, quantized, brute force) each take their own
    keyword-only search options (``ef`` / ``rerank`` / ``per_shard_k`` / ``where``).
    The protocol therefore only pins the positional ``(vector, k)`` call shape;
    callers thread backend-specific options through ``benchmark(search_kwargs=...)``.
    Functions below accept any object structurally satisfying this (typed ``Any``
    at the boundary so a backend's narrower keyword signature still qualifies).
    """

    def search(self, vector: Any, k: int = ...) -> list[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Aggregate quality + latency for one index over a query set."""

    k: int
    n_queries: int
    recall_at_k: float
    map_at_k: float
    ndcg_at_k: float
    mean_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    qps: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "k": self.k,
            "n_queries": self.n_queries,
            "recall_at_k": self.recall_at_k,
            "map_at_k": self.map_at_k,
            "ndcg_at_k": self.ndcg_at_k,
            "mean_latency_ms": self.mean_latency_ms,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "p99_latency_ms": self.p99_latency_ms,
            "qps": self.qps,
        }


def exact_ground_truth(
    ids: Sequence[VectorId],
    data: NDArray[np.float32],
    queries: NDArray[np.float32],
    k: int,
    *,
    metric: Metric = Metric.COSINE,
) -> list[list[VectorId]]:
    """Exact top-``k`` ids for each query, computed in one vectorised pass."""
    norm_data = dist.maybe_normalize_matrix(data, metric)
    norm_q = dist.maybe_normalize_matrix(queries, metric)
    order = dist.pairwise_order(norm_q, norm_data, metric)  # (m, n), smaller=closer
    truth: list[list[VectorId]] = []
    for row in order:
        top = np.argsort(row, kind="stable")[:k]
        truth.append([ids[int(i)] for i in top])
    return truth


def recall_at_k(returned: Sequence[VectorId], truth: Sequence[VectorId]) -> float:
    """Fraction of the exact top-``k`` that ``returned`` contains."""
    if not truth:
        return 1.0
    return len(set(returned) & set(truth)) / len(truth)


def average_precision(returned: Sequence[VectorId], truth: Sequence[VectorId]) -> float:
    """Average precision of a ranked list against the relevant (exact) set."""
    if not truth:
        return 1.0
    relevant = set(truth)
    hits = 0
    score = 0.0
    for rank, vid in enumerate(returned, start=1):
        if vid in relevant:
            hits += 1
            score += hits / rank
    return score / len(truth)


def ndcg(returned: Sequence[VectorId], truth: Sequence[VectorId]) -> float:
    """Binary-relevance nDCG of the returned ranking vs the exact set."""
    if not truth:
        return 1.0
    relevant = set(truth)
    dcg = sum(
        1.0 / math.log2(rank + 1) for rank, vid in enumerate(returned, start=1) if vid in relevant
    )
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(truth), len(returned)) + 1))
    return dcg / idcg if idcg > 0 else 0.0


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    return float(np.percentile(np.asarray(samples), pct))


def benchmark(
    index: Searchable,
    ids: Sequence[VectorId],
    data: Any,
    queries: Any,
    *,
    k: int = 10,
    metric: Metric = Metric.COSINE,
    search_kwargs: dict[str, Any] | None = None,
    warmup: int = 0,
) -> BenchmarkReport:
    """Benchmark ``index`` against exact ground truth over ``queries``.

    ``data`` is the full corpus (to compute exact truth); ``index`` must already
    contain it. ``search_kwargs`` are forwarded to ``index.search`` (e.g.
    ``{"ef": 128}`` or ``{"rerank": 200}``).
    """
    data_mat = as_matrix(list(data)) if not isinstance(data, np.ndarray) else data.astype(FLOAT)
    query_mat = (
        as_matrix(list(queries)) if not isinstance(queries, np.ndarray) else queries.astype(FLOAT)
    )
    truth = exact_ground_truth(ids, data_mat, query_mat, k, metric=metric)
    kwargs = search_kwargs or {}

    for _ in range(min(warmup, query_mat.shape[0])):
        index.search(query_mat[0], k, **kwargs)

    recalls: list[float] = []
    aps: list[float] = []
    ndcgs: list[float] = []
    latencies: list[float] = []
    for i in range(query_mat.shape[0]):
        q = query_mat[i]
        t0 = time.perf_counter()
        results = index.search(q, k, **kwargs)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        returned = [r.id for r in results]
        recalls.append(recall_at_k(returned, truth[i]))
        aps.append(average_precision(returned, truth[i]))
        ndcgs.append(ndcg(returned, truth[i]))

    n = query_mat.shape[0]
    total_s = sum(latencies) / 1000.0
    return BenchmarkReport(
        k=k,
        n_queries=n,
        recall_at_k=float(np.mean(recalls)) if recalls else 0.0,
        map_at_k=float(np.mean(aps)) if aps else 0.0,
        ndcg_at_k=float(np.mean(ndcgs)) if ndcgs else 0.0,
        mean_latency_ms=float(np.mean(latencies)) if latencies else 0.0,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        p99_latency_ms=_percentile(latencies, 99),
        qps=(n / total_s) if total_s > 0 else 0.0,
    )


def compare(
    targets: dict[str, Searchable],
    ids: Sequence[VectorId],
    data: Any,
    queries: Any,
    *,
    k: int = 10,
    metric: Metric = Metric.COSINE,
    search_kwargs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, BenchmarkReport]:
    """Benchmark several indexes against one ground truth and return per-name reports."""
    per = search_kwargs or {}
    return {
        name: benchmark(idx, ids, data, queries, k=k, metric=metric, search_kwargs=per.get(name))
        for name, idx in targets.items()
    }


def build_brute_force(
    ids: Sequence[VectorId], data: Any, *, metric: Metric = Metric.COSINE
) -> BruteForceIndex:
    """Convenience: a populated brute-force index for ad-hoc comparisons."""
    mat = as_matrix(list(data))
    bf = BruteForceIndex(mat.shape[1] if mat.size else 1, metric=metric)
    bf.add_many(list(ids), [mat[i] for i in range(mat.shape[0])])
    return bf


def run_demo(
    *, n: int = 5000, dim: int = 64, n_queries: int = 200, k: int = 10, seed: int = 0
) -> dict[str, BenchmarkReport]:
    """Self-contained demo: build every backend on seeded clustered data and
    benchmark each against the exact brute-force ground truth.

    Importable (returns the per-backend reports) and runnable as
    ``python -m app.datascale.vectorsearch.benchmark``.
    """
    from .builder import build_hnsw, build_quantized, build_sharded
    from .distance import normalize_matrix

    rng = np.random.default_rng(seed)
    clusters = max(8, n // 100)
    centres = rng.standard_normal((clusters, dim)).astype(FLOAT)
    labels = rng.integers(0, clusters, size=n)
    data = normalize_matrix(
        (centres[labels] + 0.18 * rng.standard_normal((n, dim))).astype(FLOAT)
    )
    ids = [f"v{i}" for i in range(n)]
    rows = [data[i] for i in range(n)]
    queries = normalize_matrix(rng.standard_normal((n_queries, dim)).astype(FLOAT))

    targets: dict[str, Searchable] = {
        "brute_force": build_brute_force(ids, rows),
        "hnsw": build_hnsw(ids, rows, dim=dim),
        "sharded(4)": build_sharded(ids, rows, n_shards=4, dim=dim),
        "pq+rerank": build_quantized(ids, rows, dim=dim, kind="pq", m=16, nbits=8),
        "sq": build_quantized(ids, rows, dim=dim, kind="sq", sq_bits=8),
    }
    skw: dict[str, dict[str, Any]] = {
        "hnsw": {"ef": 128},
        "sharded(4)": {"ef": 128, "per_shard_k": k * 2},
        "pq+rerank": {"rerank": 256},
        "sq": {"rerank": 256},
    }
    return compare(targets, ids, data, queries, k=k, search_kwargs=skw)


def _main() -> None:  # pragma: no cover - CLI entry
    reports = run_demo()
    header = f"{'backend':<14}{'recall@k':>10}{'mAP':>8}{'nDCG':>8}{'p95 ms':>10}{'qps':>10}"
    print(header)
    print("-" * len(header))
    for name, r in reports.items():
        print(
            f"{name:<14}{r.recall_at_k:>10.3f}{r.map_at_k:>8.3f}"
            f"{r.ndcg_at_k:>8.3f}{r.p95_latency_ms:>10.2f}{r.qps:>10.0f}"
        )


if __name__ == "__main__":  # pragma: no cover
    _main()


__all__ = [
    "BenchmarkReport",
    "Searchable",
    "average_precision",
    "benchmark",
    "build_brute_force",
    "compare",
    "exact_ground_truth",
    "ndcg",
    "recall_at_k",
    "run_demo",
]
