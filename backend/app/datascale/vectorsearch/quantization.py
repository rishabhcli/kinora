"""Vector compression: Product Quantization (PQ) and Scalar Quantization (SQ).

Both trade a controlled amount of recall for a large memory / disk reduction —
the lever that lets a single node hold tens of millions of vectors.

Scalar Quantization (``ScalarQuantizer``)
    Per-dimension affine map of ``float32`` → ``uint8`` (or any ``2^bits``
    levels). 4× smaller than float32, near-lossless for unit vectors, and the
    cheapest decode. Good default for the in-memory tier.

Product Quantization (``ProductQuantizer``)
    Split the ``d``-dim vector into ``m`` sub-vectors, k-means each sub-space
    into ``2^nbits`` centroids, and store the centroid id per sub-vector — so a
    vector becomes ``m`` bytes (with ``nbits=8``). Distance to a query is read
    from a precomputed ``m × 2^nbits`` **asymmetric distance table** (ADC) with
    one lookup-and-sum per code, no decompression. This is the classic
    billion-scale ANN compressor (Jégou et al., 2011).

Everything is NumPy, deterministic given a seed, and serialisable (the codebooks
are plain arrays the storage layer persists alongside the index).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .types import FLOAT, Metric

# --------------------------------------------------------------------------- #
# Scalar quantization
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ScalarQuantizer:
    """Per-dimension affine ``float32`` ↔ ``uint`` quantizer.

    Fit learns a min/max per dimension; encode maps to ``[0, levels-1]`` and
    decode maps back to the bucket centre. ``bits=8`` gives 4× compression.
    """

    bits: int = 8
    _lo: NDArray[np.float32] | None = None
    _scale: NDArray[np.float32] | None = None

    @property
    def levels(self) -> int:
        return (1 << self.bits) - 1

    @property
    def is_fitted(self) -> bool:
        return self._lo is not None

    def fit(self, data: NDArray[np.float32]) -> ScalarQuantizer:
        if data.ndim != 2 or data.shape[0] == 0:
            raise ValueError("fit expects a non-empty (n, d) matrix")
        lo = data.min(axis=0).astype(FLOAT)
        hi = data.max(axis=0).astype(FLOAT)
        span = np.where(hi > lo, hi - lo, 1.0).astype(FLOAT)
        self._lo = lo
        self._scale = (span / self.levels).astype(FLOAT)
        return self

    def encode(self, vectors: NDArray[np.float32]) -> NDArray[np.uint8]:
        if self._lo is None or self._scale is None:
            raise RuntimeError("ScalarQuantizer not fitted")
        x = np.atleast_2d(vectors)
        codes = np.round((x - self._lo) / self._scale)
        np.clip(codes, 0, self.levels, out=codes)
        dtype = np.uint8 if self.bits <= 8 else np.uint16
        return codes.astype(dtype)

    def decode(self, codes: NDArray[np.integer[Any]]) -> NDArray[np.float32]:
        if self._lo is None or self._scale is None:
            raise RuntimeError("ScalarQuantizer not fitted")
        x = np.atleast_2d(codes).astype(FLOAT)
        return (x * self._scale + self._lo).astype(FLOAT)

    def state(self) -> dict[str, Any]:
        return {
            "bits": self.bits,
            "lo": None if self._lo is None else self._lo.tolist(),
            "scale": None if self._scale is None else self._scale.tolist(),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> ScalarQuantizer:
        sq = cls(bits=int(state["bits"]))
        if state.get("lo") is not None:
            sq._lo = np.asarray(state["lo"], dtype=FLOAT)
            sq._scale = np.asarray(state["scale"], dtype=FLOAT)
        return sq


# --------------------------------------------------------------------------- #
# k-means (the PQ building block)
# --------------------------------------------------------------------------- #


def kmeans(
    data: NDArray[np.float32],
    k: int,
    *,
    iters: int = 25,
    seed: int = 0,
    tol: float = 1e-4,
) -> tuple[NDArray[np.float32], NDArray[np.int64]]:
    """Lloyd's k-means (k-means++ init). Returns ``(centroids, assignments)``.

    Deterministic given ``seed``. Empty clusters are re-seeded to the point
    farthest from its centroid so ``k`` centroids are always populated.
    """
    n = data.shape[0]
    rng = np.random.default_rng(seed)
    if n == 0:
        return np.zeros((k, data.shape[1]), dtype=FLOAT), np.zeros((0,), dtype=np.int64)
    k = min(k, n)
    centroids = _kmeans_plus_plus(data, k, rng)
    assign = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        # assignment step (squared L2)
        d2 = _sq_dists(data, centroids)
        new_assign = np.argmin(d2, axis=1)
        moved = float(np.mean(new_assign != assign)) if n else 0.0
        assign = new_assign
        # update step
        for c in range(k):
            members = data[assign == c]
            if members.shape[0] > 0:
                centroids[c] = members.mean(axis=0)
            else:  # re-seed empty cluster
                far = int(np.argmax(np.min(d2, axis=1)))
                centroids[c] = data[far]
        if moved < tol:
            break
    return centroids.astype(FLOAT), assign


def _kmeans_plus_plus(
    data: NDArray[np.float32], k: int, rng: np.random.Generator
) -> NDArray[np.float32]:
    n = data.shape[0]
    first = int(rng.integers(n))
    centroids = [data[first]]
    closest = _sq_dists(data, np.asarray(centroids)).ravel()
    for _ in range(1, k):
        total = float(closest.sum())
        if total <= 0:
            centroids.append(data[int(rng.integers(n))])
        else:
            probs = closest / total
            nxt = int(rng.choice(n, p=probs))
            centroids.append(data[nxt])
        new_d = _sq_dists(data, np.asarray([centroids[-1]])).ravel()
        closest = np.minimum(closest, new_d)
    return np.asarray(centroids, dtype=FLOAT)


def _sq_dists(x: NDArray[np.float32], c: NDArray[np.float32]) -> NDArray[np.float32]:
    """Squared L2 distances ``(n, k)`` between rows of ``x`` and centroids ``c``."""
    xx = np.einsum("ij,ij->i", x, x)[:, None]
    cc = np.einsum("ij,ij->i", c, c)[None, :]
    sq = xx + cc - 2.0 * (x @ c.T)
    np.maximum(sq, 0.0, out=sq)
    return sq.astype(FLOAT)


# --------------------------------------------------------------------------- #
# Product quantization
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ProductQuantizer:
    """Product quantizer with asymmetric distance computation (ADC).

    ``m`` sub-quantizers each over ``d/m`` dims, ``2^nbits`` centroids each.
    ``fit`` learns the codebooks; ``encode`` returns one code (centroid id) per
    sub-vector; :meth:`distance_table` builds the per-query lookup table whose
    sum over a code's ``m`` entries is the (approximate) squared-L2 distance.
    """

    m: int = 8
    nbits: int = 8
    iters: int = 25
    seed: int = 0
    _dim: int = 0
    _dsub: int = 0
    # codebooks: (m, ksub, dsub)
    _codebooks: NDArray[np.float32] | None = None

    @property
    def ksub(self) -> int:
        return 1 << self.nbits

    @property
    def is_fitted(self) -> bool:
        return self._codebooks is not None

    def fit(self, data: NDArray[np.float32]) -> ProductQuantizer:
        if data.ndim != 2 or data.shape[0] == 0:
            raise ValueError("fit expects a non-empty (n, d) matrix")
        d = data.shape[1]
        if d % self.m != 0:
            raise ValueError(f"dim {d} not divisible by m={self.m}")
        self._dim = d
        self._dsub = d // self.m
        books = np.zeros((self.m, self.ksub, self._dsub), dtype=FLOAT)
        for sub in range(self.m):
            block = data[:, sub * self._dsub : (sub + 1) * self._dsub]
            centroids, _ = kmeans(
                np.ascontiguousarray(block),
                self.ksub,
                iters=self.iters,
                seed=self.seed + sub,
            )
            # kmeans may return < ksub rows if n < ksub; pad by repeating.
            if centroids.shape[0] < self.ksub:
                pad = self.ksub - centroids.shape[0]
                centroids = np.vstack([centroids, np.repeat(centroids[-1:], pad, axis=0)])
            books[sub] = centroids
        self._codebooks = books
        return self

    def encode(self, vectors: NDArray[np.float32]) -> NDArray[np.uint8]:
        if self._codebooks is None:
            raise RuntimeError("ProductQuantizer not fitted")
        x = np.atleast_2d(vectors)
        n = x.shape[0]
        dtype = np.uint8 if self.nbits <= 8 else np.uint16
        codes = np.zeros((n, self.m), dtype=dtype)
        for sub in range(self.m):
            block = x[:, sub * self._dsub : (sub + 1) * self._dsub]
            d2 = _sq_dists(np.ascontiguousarray(block), self._codebooks[sub])
            codes[:, sub] = np.argmin(d2, axis=1)
        return codes

    def decode(self, codes: NDArray[np.integer[Any]]) -> NDArray[np.float32]:
        if self._codebooks is None:
            raise RuntimeError("ProductQuantizer not fitted")
        c = np.atleast_2d(codes)
        n = c.shape[0]
        out = np.zeros((n, self._dim), dtype=FLOAT)
        for sub in range(self.m):
            out[:, sub * self._dsub : (sub + 1) * self._dsub] = self._codebooks[sub][c[:, sub]]
        return out

    def distance_table(self, query: NDArray[np.float32]) -> NDArray[np.float32]:
        """ADC table ``(m, ksub)``: squared-L2 of each query sub-vector to each centroid."""
        if self._codebooks is None:
            raise RuntimeError("ProductQuantizer not fitted")
        q = np.asarray(query, dtype=FLOAT).ravel()
        table = np.zeros((self.m, self.ksub), dtype=FLOAT)
        for sub in range(self.m):
            qsub = q[sub * self._dsub : (sub + 1) * self._dsub]
            diff = self._codebooks[sub] - qsub
            table[sub] = np.einsum("ij,ij->i", diff, diff)
        return table

    def adc_distances(
        self, query: NDArray[np.float32], codes: NDArray[np.integer[Any]]
    ) -> NDArray[np.float32]:
        """Approximate squared-L2 distances from ``query`` to each coded vector.

        One table build + a gather-and-sum over codes — no decompression. This
        is the hot path a PQ-backed index uses to score its candidate pool.
        """
        table = self.distance_table(query)
        c = np.atleast_2d(codes)
        # sum over sub-quantizers: table[sub, code[:, sub]]
        cols = np.arange(self.m)
        return table[cols, c].sum(axis=1).astype(FLOAT)

    def reconstruction_error(self, data: NDArray[np.float32]) -> float:
        """Mean squared reconstruction error — a quality knob for tests/tuning."""
        codes = self.encode(data)
        recon = self.decode(codes)
        diff = data - recon
        return float(np.mean(np.einsum("ij,ij->i", diff, diff)))

    def state(self) -> dict[str, Any]:
        return {
            "m": self.m,
            "nbits": self.nbits,
            "dim": self._dim,
            "dsub": self._dsub,
            "codebooks": None if self._codebooks is None else self._codebooks.tolist(),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> ProductQuantizer:
        pq = cls(m=int(state["m"]), nbits=int(state["nbits"]))
        pq._dim = int(state["dim"])
        pq._dsub = int(state["dsub"])
        if state.get("codebooks") is not None:
            pq._codebooks = np.asarray(state["codebooks"], dtype=FLOAT)
        return pq


def metric_uses_l2(metric: Metric) -> bool:
    """PQ's ADC is squared-L2; cosine/dot reduce to it on normalised vectors.

    For normalised vectors, ``||q - x||^2 = 2 - 2·cos`` so the L2 ordering equals
    the cosine ordering — meaning a PQ ADC over normalised vectors is a valid
    re-rank key for the cosine metric too. This helper documents that contract.
    """
    return True


__all__ = [
    "ProductQuantizer",
    "ScalarQuantizer",
    "kmeans",
    "metric_uses_l2",
]
