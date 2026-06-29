"""Unit tests for the unsupervised-statistics primitives (defense.stats)."""

from __future__ import annotations

import math

import pytest

from app.zerotrust.defense.stats import (
    Ewma,
    IsolationForestLite,
    Mad,
    RobustScaler,
    clamp01,
    logistic,
)


def test_logistic_is_monotone_and_bounded() -> None:
    assert logistic(0.0) == pytest.approx(0.5)
    assert 0.0 < logistic(-50.0) < logistic(0.0) < logistic(2.0) <= 1.0
    # Numerically stable at extremes (no overflow; saturates to the bound).
    assert logistic(1e3) == pytest.approx(1.0)
    assert logistic(-1e3) == pytest.approx(0.0)


def test_clamp01() -> None:
    assert clamp01(-0.4) == 0.0
    assert clamp01(1.7) == 1.0
    assert clamp01(0.3) == 0.3


def test_ewma_converges_to_constant_signal() -> None:
    e = Ewma(half_life=10.0)
    for i in range(200):
        e.observe(5.0, float(i))
    assert e.mean == pytest.approx(5.0, abs=1e-6)
    assert e.stddev == pytest.approx(0.0, abs=1e-6)


def test_ewma_zscore_flags_a_spike() -> None:
    e = Ewma(half_life=30.0)
    t = 0.0
    for _ in range(100):
        e.observe(10.0 + ((t * 7) % 3 - 1), t)  # small deterministic jitter
        t += 1.0
    # A 10x spike is many sigma above the baseline.
    assert e.zscore(120.0) > 5.0


def test_ewma_rejects_bad_half_life() -> None:
    with pytest.raises(ValueError):
        Ewma(half_life=0.0)


def test_mad_robust_z_ignores_outliers() -> None:
    m = Mad(window=64)
    # A spread-out baseline so the MAD scale is non-degenerate.
    for i in range(60):
        m.observe(100.0 + (i % 5))
    m.observe(1_000_000.0)  # a single wild outlier
    # Median is unmoved by one outlier; a value near the median is ~0.
    assert abs(m.robust_z(102.0)) < 2.0
    # The outlier itself is extremely anomalous (many robust sigmas out).
    assert m.robust_z(1_000_000.0) > 50.0


def test_mad_robust_z_degenerate_scale_caps() -> None:
    # When every point is identical the MAD scale is 0; a departure is reported
    # as a fixed +/-6 sentinel rather than a divide-by-zero.
    m = Mad(window=64)
    for _ in range(60):
        m.observe(100.0)
    m.observe(100.0)
    assert m.robust_z(1_000_000.0) == pytest.approx(6.0)
    assert m.robust_z(100.0) == 0.0


def test_mad_degenerate_scale() -> None:
    m = Mad(window=16)
    for _ in range(16):
        m.observe(3.0)
    assert m.robust_z(3.0) == 0.0
    assert m.robust_z(9.0) == pytest.approx(6.0)
    assert m.robust_z(-1.0) == pytest.approx(-6.0)


def test_mad_not_ready_returns_zero() -> None:
    m = Mad(window=8)
    m.observe(1.0)
    assert not m.ready
    assert m.robust_z(99.0) == 0.0


def test_robust_scaler_quiet_then_burst() -> None:
    s = RobustScaler(half_life=60.0, window=64)
    t = 0.0
    for _ in range(80):
        s.update(2.0, t)
        t += 1.0
    quiet = s.score(2.0)
    burst = s.score(60.0)
    assert quiet < 0.2
    assert burst > 0.8
    assert burst > quiet


def test_robust_scaler_one_sided_ignores_dips() -> None:
    s = RobustScaler(half_life=60.0, window=64, two_sided=False)
    t = 0.0
    for _ in range(80):
        s.update(50.0, t)
        t += 1.0
    # A drop below baseline is not anomalous for a one-sided rate signal.
    assert s.score(1.0) == pytest.approx(clamp01(logistic(-3.0, k=0.6)), abs=1e-9)
    assert s.score(1.0) < 0.2


def test_isolation_forest_scores_outlier_higher() -> None:
    rng_inliers = [[math.sin(i), math.cos(i)] for i in range(300)]
    forest = IsolationForestLite(n_trees=64, sample_size=128, seed=42).fit(rng_inliers)
    inlier_score = forest.score([0.0, 1.0])
    outlier_score = forest.score([50.0, -50.0])
    assert outlier_score > inlier_score
    assert outlier_score > 0.6


def test_isolation_forest_is_deterministic_given_seed() -> None:
    data = [[float(i % 5), float((i * 3) % 7)] for i in range(200)]
    a = IsolationForestLite(n_trees=32, sample_size=64, seed=1).fit(data)
    b = IsolationForestLite(n_trees=32, sample_size=64, seed=1).fit(data)
    pt = [12.0, -8.0]
    assert a.score(pt) == b.score(pt)


def test_isolation_forest_novelty_catches_constant_dim_anomaly() -> None:
    # Dim 1 is constant (1.0) in training -> trees are blind to it, but a query
    # far outside its support must still be flagged via the novelty term.
    import random

    rng = random.Random(5)
    data = [[rng.uniform(0.0, 0.2), 1.0] for _ in range(200)]
    f = IsolationForestLite(n_trees=32, sample_size=64, seed=9).fit(data)
    assert f.score([0.1, 1.0]) < 0.6  # in support on both dims
    assert f.score([0.1, 40.0]) > 0.9  # gross excursion on the constant dim
    # Boundary grazing on a constant-zero dimension is NOT anomalous.
    zdata = [[rng.uniform(0.0, 0.2), 0.0] for _ in range(200)]
    g = IsolationForestLite(n_trees=32, sample_size=64, seed=9).fit(zdata)
    assert g.novelty([0.1, 0.05]) < 0.3


def test_isolation_forest_novelty_zero_in_support() -> None:
    import random

    rng = random.Random(1)
    data = [[rng.uniform(0.0, 1.0)] for _ in range(100)]
    f = IsolationForestLite(n_trees=16, sample_size=32, seed=2).fit(data)
    assert f.novelty([0.5]) == 0.0
    with pytest.raises(RuntimeError):
        IsolationForestLite().novelty([1.0])


def test_isolation_forest_validation() -> None:
    with pytest.raises(ValueError):
        IsolationForestLite(n_trees=0)
    with pytest.raises(ValueError):
        IsolationForestLite(sample_size=1)
    f = IsolationForestLite()
    with pytest.raises(RuntimeError):
        f.score([1.0, 2.0])
    with pytest.raises(ValueError):
        f.fit([])
    with pytest.raises(ValueError):
        f.fit([[1.0, 2.0], [3.0]])  # ragged
