"""Tests for the exact brute-force index (the recall ground truth itself)."""

from __future__ import annotations

import numpy as np
import pytest

from app.datascale.vectorsearch import distance as dist
from app.datascale.vectorsearch.brute_force import BruteForceIndex
from app.datascale.vectorsearch.types import FLOAT, Metric

from .conftest import Corpus


def test_brute_force_is_exact_against_numpy(clustered: Corpus) -> None:
    bf = BruteForceIndex(clustered.dim, metric=Metric.COSINE)
    bf.add_many(clustered.ids, clustered.rows())
    q = clustered.queries[0]
    got = [r.id for r in bf.search(q, 10)]
    # Independent exact computation.
    sims = clustered.vectors @ dist.normalize(q)
    truth = [clustered.ids[int(i)] for i in np.argsort(-sims)[:10]]
    assert got == truth


def test_brute_force_l2_metric(isotropic: Corpus) -> None:
    bf = BruteForceIndex(isotropic.dim, metric=Metric.L2)
    bf.add_many(isotropic.ids, isotropic.rows())
    q = isotropic.queries[0]
    got = [r.id for r in bf.search(q, 5)]
    d2 = np.sum((isotropic.vectors - q) ** 2, axis=1)
    truth = [isotropic.ids[int(i)] for i in np.argsort(d2)[:5]]
    assert got == truth


def test_remove_then_absent() -> None:
    bf = BruteForceIndex(4)
    bf.add("a", [1, 0, 0, 0])
    bf.add("b", [0, 1, 0, 0])
    assert "a" in bf and len(bf) == 2
    assert bf.remove("a") is True
    assert "a" not in bf and len(bf) == 1
    assert bf.remove("a") is False  # idempotent


def test_swap_remove_keeps_other_vectors_findable() -> None:
    rng = np.random.default_rng(5)
    bf = BruteForceIndex(8)
    vecs = {f"k{i}": rng.standard_normal(8).astype(FLOAT) for i in range(20)}
    for k, v in vecs.items():
        bf.add(k, v)
    bf.remove("k0")
    bf.remove("k10")
    for k in ("k1", "k5", "k19"):
        assert k in bf
        stored = bf.get(k)
        assert stored is not None
        assert np.allclose(stored, dist.normalize(vecs[k]), atol=1e-6)


def test_metadata_prefilter() -> None:
    bf = BruteForceIndex(3)
    for i in range(10):
        bf.add(f"x{i}", [float(i), 0.0, 0.0], metadata={"g": i % 2})
    res = bf.search([5.0, 0.0, 0.0], 10, where={"g": 1})
    assert all(r.metadata is not None and r.metadata["g"] == 1 for r in res)
    assert len(res) == 5


def test_replace_updates_vector() -> None:
    bf = BruteForceIndex(2)
    bf.add("a", [1.0, 0.0])
    bf.add("a", [0.0, 1.0])  # replace
    assert len(bf) == 1
    res = bf.search([0.0, 1.0], 1)
    assert res[0].id == "a" and res[0].score > 0.99


def test_empty_and_zero_k() -> None:
    bf = BruteForceIndex(4)
    assert bf.search([1, 0, 0, 0], 5) == []
    bf.add("a", [1, 0, 0, 0])
    assert bf.search([1, 0, 0, 0], 0) == []


def test_dim_mismatch_rejected() -> None:
    bf = BruteForceIndex(4)
    with pytest.raises(ValueError):
        bf.add("a", [1, 2, 3])
