"""Offline/online parity validation + training-serving skew detection."""

from __future__ import annotations

import pytest

from app.lakehouse.features import (
    FeatureSpec,
    ValueType,
    check_parity,
    detect_skew,
    population_stability_index,
)
from app.lakehouse.features.parity import categorical_linf

_SPECS = (
    FeatureSpec(name="pages_read", dtype=ValueType.INT, default=0),
    FeatureSpec(name="score", dtype=ValueType.FLOAT, default=0.0),
    FeatureSpec(name="genre", dtype=ValueType.STRING, default="unknown"),
    FeatureSpec(name="embedding", dtype=ValueType.FLOAT_VECTOR, default=None),
)


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #


def test_parity_all_match() -> None:
    offline = {
        "u1": {"pages_read": 5, "score": 0.5, "genre": "scifi", "embedding": [1.0, 2.0]},
        "u2": {"pages_read": 9, "score": 0.9, "genre": "fantasy", "embedding": [3.0, 4.0]},
    }
    online = {k: dict(v) for k, v in offline.items()}
    report = check_parity(_SPECS, offline=offline, online=online)
    assert report.ok
    assert report.overall_match_rate == 1.0


def test_parity_float_tolerance() -> None:
    offline = {"u1": {"score": 0.1 + 0.2}}  # 0.30000000000000004
    online = {"u1": {"score": 0.3}}
    report = check_parity([_SPECS[1]], offline=offline, online=online)
    assert report.ok  # within rel_tol


def test_parity_detects_mismatch() -> None:
    offline = {"u1": {"pages_read": 5}, "u2": {"pages_read": 9}}
    online = {"u1": {"pages_read": 5}, "u2": {"pages_read": 999}}
    report = check_parity([_SPECS[0]], offline=offline, online=online)
    assert not report.ok
    feat = report.feature("pages_read")
    assert feat.compared == 2 and feat.matches == 1
    assert feat.match_rate == 0.5
    assert feat.mismatches[0] == ("u2", 9, 999)


def test_parity_vector_order_sensitive_but_value_tolerant() -> None:
    offline = {"u1": {"embedding": [1.0, 2.0, 3.0]}}
    online = {"u1": {"embedding": [1.0000001, 2.0, 3.0]}}
    report = check_parity([_SPECS[3]], offline=offline, online=online)
    assert report.ok
    online_bad = {"u1": {"embedding": [9.0, 2.0, 3.0]}}
    assert not check_parity([_SPECS[3]], offline=offline, online=online_bad).ok


def test_parity_only_shared_keys_compared() -> None:
    offline = {"u1": {"pages_read": 5}, "u2": {"pages_read": 9}}
    online = {"u1": {"pages_read": 5}}  # u2 not materialised yet
    report = check_parity([_SPECS[0]], offline=offline, online=online)
    assert report.feature("pages_read").compared == 1  # only u1
    assert report.ok


# --------------------------------------------------------------------------- #
# Skew / drift
# --------------------------------------------------------------------------- #


def test_psi_zero_for_identical_distributions() -> None:
    sample = [float(i % 10) for i in range(200)]
    assert population_stability_index(sample, list(sample)) == pytest.approx(0.0, abs=1e-9)


def test_psi_large_for_shifted_distribution() -> None:
    reference = [float(i) for i in range(100)]  # 0..99
    current = [float(i + 1000) for i in range(100)]  # 1000..1099 (fully shifted)
    psi = population_stability_index(reference, current)
    assert psi > 0.25  # "large" drift


def test_categorical_linf() -> None:
    ref = ["a"] * 50 + ["b"] * 50
    cur = ["a"] * 90 + ["b"] * 10
    dist = categorical_linf(ref, cur)
    assert dist == pytest.approx(0.4, abs=1e-9)  # |0.5-0.9| = 0.4


def test_detect_skew_flags_drifted_numeric() -> None:
    specs = [FeatureSpec(name="score", dtype=ValueType.FLOAT)]
    reference = {"score": [float(i) for i in range(100)]}
    current = {"score": [float(i + 1000) for i in range(100)]}
    report = detect_skew(specs, reference=reference, current=current)
    assert "score" in report.drifted_features
    assert report.feature("score").method == "psi"
    assert report.feature("score").severity == "large"
    assert not report.ok


def test_detect_skew_stable_when_no_drift() -> None:
    specs = [FeatureSpec(name="score", dtype=ValueType.FLOAT)]
    sample = [float(i % 7) for i in range(140)]
    report = detect_skew(specs, reference={"score": sample}, current={"score": list(sample)})
    assert report.ok
    assert report.feature("score").severity == "stable"


def test_detect_skew_vector_feature_has_no_score() -> None:
    specs = [FeatureSpec(name="embedding", dtype=ValueType.FLOAT_VECTOR)]
    report = detect_skew(
        specs,
        reference={"embedding": [[1.0, 2.0]]},
        current={"embedding": [[3.0, 4.0]]},
    )
    assert report.feature("embedding").method == "none"
    assert report.feature("embedding").score == 0.0


def test_detect_skew_categorical_distribution_shift() -> None:
    specs = [FeatureSpec(name="genre", dtype=ValueType.STRING)]
    reference = {"genre": ["a"] * 50 + ["b"] * 50}
    current = {"genre": ["a"] * 95 + ["b"] * 5}
    report = detect_skew(specs, reference=reference, current=current, moderate=0.1)
    assert "genre" in report.drifted_features
    assert report.feature("genre").method == "linf"
