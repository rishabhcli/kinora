"""QA distribution-drift monitoring — fleet-level novel-failure detection (§9.5).

The per-clip anomaly detector (``app/render/reward.py``) flags *one* out-of-place QA
vector. This module watches the *stream* of QA scores over time and flags when the
whole distribution shifts — a silent regression where the renderer (or a provider
model update) starts producing systematically worse clips, or where a new book's
visual style moves the baseline. That is the kind of novel failure mode no fixed
threshold catches, because every individual clip can still pass while the *population*
degrades.

Two complementary signals, both pure and deterministic:

* **Population Stability Index (PSI)** — the standard model-monitoring metric for "has
  this feature's distribution moved" between a reference window and a recent window.
  PSI < 0.1 = stable, 0.1–0.25 = moderate shift, > 0.25 = significant shift.
* **Windowed mean shift** — a simple, interpretable "the recent mean of axis X moved
  by D from the reference mean", with the direction (improving / degrading).

A :class:`DriftReport` rolls the per-axis signals up to one verdict so a Phase-5
monitor can alert and trigger a re-calibration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import log

from app.render.reward import FEATURE_NAMES, QASample

#: PSI bands (the industry-standard model-drift thresholds).
PSI_STABLE = 0.1
PSI_SIGNIFICANT = 0.25
#: Number of equal-width bins over the 0..1 feature range for the PSI histogram.
_PSI_BINS = 10
#: Minimum window size before drift is computed (too few = noise).
MIN_WINDOW = 10


@dataclass(frozen=True, slots=True)
class AxisDrift:
    """Drift of one QA feature axis between a reference and a recent window."""

    axis: str
    psi: float
    reference_mean: float
    recent_mean: float
    mean_shift: float
    degrading: bool


@dataclass(frozen=True, slots=True)
class DriftReport:
    """The fleet-level QA drift verdict across all feature axes."""

    per_axis: list[AxisDrift] = field(default_factory=list)
    max_psi: float = 0.0
    drifted: bool = False
    worst_axis: str | None = None
    n_reference: int = 0
    n_recent: int = 0


def _histogram(values: Sequence[float], bins: int) -> list[float]:
    """Fractional counts per equal-width bin over [0, 1] (sums to 1)."""
    counts = [0.0] * bins
    if not values:
        return counts
    for v in values:
        idx = min(bins - 1, max(0, int(v * bins)))
        counts[idx] += 1.0
    n = float(len(values))
    return [c / n for c in counts]


def population_stability_index(
    reference: Sequence[float], recent: Sequence[float], *, bins: int = _PSI_BINS
) -> float:
    """PSI between a reference and a recent sample of one axis (pure).

    ``PSI = Σ (recent_i − ref_i) · ln(recent_i / ref_i)`` over the histogram bins, with
    a small epsilon so an empty bin doesn't blow up the log. Symmetric and ≥ 0; bigger
    = more drift.
    """
    if not reference or not recent:
        return 0.0
    ref_hist = _histogram(reference, bins)
    rec_hist = _histogram(recent, bins)
    eps = 1e-6
    psi = 0.0
    for ref_p, rec_p in zip(ref_hist, rec_hist, strict=True):
        r = max(ref_p, eps)
        c = max(rec_p, eps)
        psi += (c - r) * log(c / r)
    return round(psi, 6)


def _axis_values(samples: Sequence[QASample], axis_index: int) -> list[float]:
    return [s.features()[axis_index] for s in samples]


def detect_drift(
    reference: Sequence[QASample],
    recent: Sequence[QASample],
    *,
    psi_threshold: float = PSI_SIGNIFICANT,
) -> DriftReport:
    """Compare a reference window of QA samples to a recent window, per axis (pure).

    Returns a :class:`DriftReport`; ``drifted`` is true when any axis's PSI exceeds
    ``psi_threshold``. ``degrading`` per axis means the recent mean moved in the *bad*
    direction (all features are 0..1 goodness, so a lower recent mean = degrading).
    """
    if len(reference) < MIN_WINDOW or len(recent) < MIN_WINDOW:
        return DriftReport(n_reference=len(reference), n_recent=len(recent))

    per_axis: list[AxisDrift] = []
    max_psi = 0.0
    worst_axis: str | None = None
    for i, name in enumerate(FEATURE_NAMES):
        ref_vals = _axis_values(reference, i)
        rec_vals = _axis_values(recent, i)
        psi = population_stability_index(ref_vals, rec_vals)
        ref_mean = sum(ref_vals) / len(ref_vals)
        rec_mean = sum(rec_vals) / len(rec_vals)
        shift = round(rec_mean - ref_mean, 6)
        per_axis.append(
            AxisDrift(
                axis=name,
                psi=psi,
                reference_mean=round(ref_mean, 6),
                recent_mean=round(rec_mean, 6),
                mean_shift=shift,
                degrading=shift < 0.0,
            )
        )
        if psi > max_psi:
            max_psi = psi
            worst_axis = name

    return DriftReport(
        per_axis=per_axis,
        max_psi=round(max_psi, 6),
        drifted=max_psi > psi_threshold,
        worst_axis=worst_axis,
        n_reference=len(reference),
        n_recent=len(recent),
    )


__all__ = [
    "MIN_WINDOW",
    "PSI_SIGNIFICANT",
    "PSI_STABLE",
    "AxisDrift",
    "DriftReport",
    "detect_drift",
    "population_stability_index",
]
