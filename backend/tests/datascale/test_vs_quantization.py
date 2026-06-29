"""Tests for product + scalar quantization and the k-means primitive."""

from __future__ import annotations

import numpy as np
import pytest

from app.datascale.vectorsearch import distance as dist
from app.datascale.vectorsearch.quantization import (
    ProductQuantizer,
    ScalarQuantizer,
    kmeans,
)
from app.datascale.vectorsearch.types import FLOAT

from .conftest import Corpus

# --------------------------------------------------------------------------- #
# k-means
# --------------------------------------------------------------------------- #


def test_kmeans_recovers_planted_clusters() -> None:
    rng = np.random.default_rng(0)
    centres = np.array([[0, 0], [10, 10], [0, 10]], dtype=FLOAT)
    data = np.vstack([c + 0.3 * rng.standard_normal((100, 2)).astype(FLOAT) for c in centres])
    cents, assign = kmeans(data, 3, seed=1)
    # Each planted centre should be close to some learned centroid.
    for c in centres:
        d = np.min(np.sum((cents - c) ** 2, axis=1))
        assert d < 1.0
    assert set(assign.tolist()) == {0, 1, 2}


def test_kmeans_deterministic() -> None:
    rng = np.random.default_rng(2)
    data = rng.standard_normal((200, 5)).astype(FLOAT)
    a_c, a_a = kmeans(data, 8, seed=7)
    b_c, b_a = kmeans(data, 8, seed=7)
    assert np.array_equal(a_a, b_a)
    assert np.allclose(a_c, b_c)


def test_kmeans_handles_k_larger_than_n() -> None:
    data = np.array([[1.0, 1.0], [2.0, 2.0]], dtype=FLOAT)
    cents, assign = kmeans(data, 10, seed=0)
    assert cents.shape[0] <= 2


# --------------------------------------------------------------------------- #
# Scalar quantization
# --------------------------------------------------------------------------- #


def test_scalar_quantizer_near_lossless(clustered: Corpus) -> None:
    sq = ScalarQuantizer(bits=8).fit(clustered.vectors)
    codes = sq.encode(clustered.vectors)
    assert codes.dtype == np.uint8
    recon = sq.decode(codes)
    mse = float(np.mean(np.sum((clustered.vectors - recon) ** 2, axis=1)))
    assert mse < 1e-3  # 8-bit SQ on unit vectors is near-lossless


def test_scalar_quantizer_compression_and_dtype_16bit() -> None:
    rng = np.random.default_rng(1)
    data = rng.standard_normal((50, 12)).astype(FLOAT)
    sq = ScalarQuantizer(bits=16).fit(data)
    codes = sq.encode(data)
    assert codes.dtype == np.uint16


def test_scalar_quantizer_requires_fit() -> None:
    sq = ScalarQuantizer(bits=8)
    with pytest.raises(RuntimeError):
        sq.encode(np.zeros((1, 4), dtype=FLOAT))


def test_scalar_quantizer_state_round_trip(clustered: Corpus) -> None:
    sq = ScalarQuantizer(bits=8).fit(clustered.vectors)
    sq2 = ScalarQuantizer.from_state(sq.state())
    a = sq.decode(sq.encode(clustered.vectors[:10]))
    b = sq2.decode(sq2.encode(clustered.vectors[:10]))
    assert np.allclose(a, b)


# --------------------------------------------------------------------------- #
# Product quantization
# --------------------------------------------------------------------------- #


def test_product_quantizer_codes_shape_and_dtype(clustered: Corpus) -> None:
    pq = ProductQuantizer(m=8, nbits=8, iters=10, seed=1).fit(clustered.vectors)
    codes = pq.encode(clustered.vectors)
    assert codes.shape == (clustered.n, 8)
    assert codes.dtype == np.uint8


def test_product_quantizer_dim_divisibility() -> None:
    pq = ProductQuantizer(m=7, nbits=4)
    with pytest.raises(ValueError):
        pq.fit(np.zeros((10, 48), dtype=FLOAT))  # 48 % 7 != 0


def test_product_quantizer_low_recon_error_on_clusters(clustered: Corpus) -> None:
    pq = ProductQuantizer(m=16, nbits=8, iters=20, seed=2).fit(clustered.vectors)
    err = pq.reconstruction_error(clustered.vectors)
    assert err < 0.1  # clustered data compresses well


def test_adc_ranks_like_exact_l2_on_clusters(clustered: Corpus) -> None:
    """ADC distance must rank candidates close to exact squared-L2 on real data."""
    pq = ProductQuantizer(m=16, nbits=8, iters=20, seed=3).fit(clustered.vectors)
    codes = pq.encode(clustered.vectors)
    # Coarse-filter recipe: top-200 by ADC then exact re-rank recovers top-10.
    recalls = []
    for qi in range(20):
        q = clustered.vectors[qi]  # query a stored vector
        adc = pq.adc_distances(q, codes)
        exact = np.sum((clustered.vectors - q) ** 2, axis=1)
        exact_top = set(np.argsort(exact)[:10].tolist())
        cand = np.argsort(adc)[:200]
        rr = set(cand[np.argsort(exact[cand])[:10]].tolist())
        recalls.append(len(exact_top & rr) / 10)
    assert float(np.mean(recalls)) >= 0.95


def test_adc_matches_decode_then_score() -> None:
    rng = np.random.default_rng(4)
    data = dist.normalize_matrix(rng.standard_normal((500, 16)).astype(FLOAT))
    pq = ProductQuantizer(m=4, nbits=6, iters=15, seed=5).fit(data)
    codes = pq.encode(data)
    q = data[0]
    adc = pq.adc_distances(q, codes)
    # ADC approximates ||q - decode(code)||^2; check it equals the explicit sum.
    recon = pq.decode(codes)
    explicit = np.sum((recon - q) ** 2, axis=1)
    assert np.allclose(adc, explicit, atol=1e-3)


def test_product_quantizer_state_round_trip(clustered: Corpus) -> None:
    pq = ProductQuantizer(m=8, nbits=8, iters=10, seed=6).fit(clustered.vectors)
    pq2 = ProductQuantizer.from_state(pq.state())
    a = pq.encode(clustered.vectors[:20])
    b = pq2.encode(clustered.vectors[:20])
    assert np.array_equal(a, b)


def test_product_quantizer_requires_fit() -> None:
    pq = ProductQuantizer(m=4, nbits=8)
    with pytest.raises(RuntimeError):
        pq.encode(np.zeros((1, 16), dtype=FLOAT))
