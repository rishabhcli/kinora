"""QA distribution-drift monitoring — PSI + windowed mean shift."""

from __future__ import annotations

from app.render.qa.drift import (
    PSI_SIGNIFICANT,
    detect_drift,
    population_stability_index,
)
from app.render.reward import QASample


def _samples(ccs: float, n: int = 30) -> list[QASample]:
    return [QASample(ccs, 0.03, True, 0.08, accepted=True) for _ in range(n)]


# --------------------------------------------------------------------------- #
# PSI
# --------------------------------------------------------------------------- #


def test_psi_identical_is_zero() -> None:
    ref = [0.9] * 50
    assert population_stability_index(ref, ref) == 0.0


def test_psi_shifted_is_positive() -> None:
    ref = [0.9] * 50
    recent = [0.4] * 50  # whole distribution moved to another bin
    assert population_stability_index(ref, recent) > PSI_SIGNIFICANT


def test_psi_empty() -> None:
    assert population_stability_index([], [0.5]) == 0.0


# --------------------------------------------------------------------------- #
# detect_drift
# --------------------------------------------------------------------------- #


def test_no_drift_on_stable_stream() -> None:
    report = detect_drift(_samples(0.92), _samples(0.92))
    assert report.drifted is False
    assert report.max_psi < 0.1


def test_drift_flagged_on_degradation() -> None:
    # The renderer silently regresses: recent CCS collapses from 0.92 to 0.55.
    report = detect_drift(_samples(0.92), _samples(0.55))
    assert report.drifted is True
    assert report.worst_axis == "ccs"
    # The CCS axis is degrading (recent mean lower than reference).
    ccs_axis = next(a for a in report.per_axis if a.axis == "ccs")
    assert ccs_axis.degrading is True
    assert ccs_axis.mean_shift < 0


def test_drift_needs_minimum_window() -> None:
    report = detect_drift(_samples(0.9, n=5), _samples(0.5, n=5))
    assert report.drifted is False  # too few samples to judge
    assert report.per_axis == []


def test_drift_report_records_window_sizes() -> None:
    report = detect_drift(_samples(0.92, n=40), _samples(0.92, n=20))
    assert report.n_reference == 40
    assert report.n_recent == 20
