"""Shared deterministic fixtures for the vector-search tests.

Everything seeds NumPy's ``default_rng`` so every recall assertion is reproducible
run-to-run. Two corpus shapes are provided: clustered (realistic, what the
quantizers want) and isotropic (the adversarial worst case).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest
from numpy.typing import NDArray

from app.datascale.vectorsearch import distance as dist
from app.datascale.vectorsearch.types import FLOAT


@dataclass(frozen=True)
class Corpus:
    """A seeded dataset: ids, normalised vectors, metadata and held-out queries."""

    ids: list[str]
    vectors: NDArray[np.float32]
    metadatas: list[dict[str, object]]
    queries: NDArray[np.float32]

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1])

    @property
    def n(self) -> int:
        return int(self.vectors.shape[0])

    def rows(self) -> list[NDArray[np.float32]]:
        return [self.vectors[i] for i in range(self.n)]


def make_clustered(
    *, n: int = 2000, dim: int = 48, clusters: int = 32, n_queries: int = 50, seed: int = 7
) -> Corpus:
    """A clustered corpus — vectors near one of ``clusters`` centres."""
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((clusters, dim)).astype(FLOAT)
    labels = rng.integers(0, clusters, size=n)
    raw = centres[labels] + 0.18 * rng.standard_normal((n, dim)).astype(FLOAT)
    vectors = dist.normalize_matrix(raw.astype(FLOAT))
    ids = [f"v{i}" for i in range(n)]
    metadatas: list[dict[str, object]] = [
        {
            "cluster": int(labels[i]),
            "book": f"book_{i % 5}",
            "page": int(i % 100),
            "even": (i % 2 == 0),
            "text": f"cluster {int(labels[i])} book {i % 5} page {i % 100}",
        }
        for i in range(n)
    ]
    queries = dist.normalize_matrix(rng.standard_normal((n_queries, dim)).astype(FLOAT))
    return Corpus(ids=ids, vectors=vectors, metadatas=metadatas, queries=queries)


def make_isotropic(*, n: int = 1500, dim: int = 32, n_queries: int = 40, seed: int = 13) -> Corpus:
    """An isotropic Gaussian corpus — the hard case (no cluster structure)."""
    rng = np.random.default_rng(seed)
    vectors = dist.normalize_matrix(rng.standard_normal((n, dim)).astype(FLOAT))
    ids = [f"u{i}" for i in range(n)]
    metadatas: list[dict[str, object]] = [
        {"idx": i, "even": (i % 2 == 0)} for i in range(n)
    ]
    queries = dist.normalize_matrix(rng.standard_normal((n_queries, dim)).astype(FLOAT))
    return Corpus(ids=ids, vectors=vectors, metadatas=metadatas, queries=queries)


@pytest.fixture(scope="session")
def clustered() -> Corpus:
    return make_clustered()


@pytest.fixture(scope="session")
def isotropic() -> Corpus:
    return make_isotropic()


@pytest.fixture(scope="session")
def small_clustered() -> Corpus:
    return make_clustered(n=600, dim=24, clusters=12, n_queries=30, seed=3)
