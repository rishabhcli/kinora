"""Distribution-drift checks between two dataset versions.

When a new dataset version is cut, the platform asks: *did the data shift in a
way that should block training?* This module compares a **reference** dataset
(the last accepted version, the training baseline) against a **candidate** (the
new cut) and reports per-feature drift with severities, so the version store can
gate a promotion and the eval can explain a regression as a data shift rather
than a model regression.

Metrics (all pure, dependency-free):

* **Categorical** — Population Stability Index (PSI) and Jensen-Shannon
  divergence over a categorical distribution (role / task / label / model mix).
  PSI is the industry-standard drift gauge: < 0.1 stable, 0.1–0.25 moderate,
  > 0.25 significant.
* **Numeric** — a normalized mean shift + a two-sample, empirical-CDF
  Kolmogorov–Smirnov statistic over reward / output-length distributions.

The output (:class:`DriftReport`) carries per-feature :class:`DriftMetric`
objects and an overall :class:`DriftSeverity`, computed from the worst feature.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.mlplatform.datasets.contracts import Dataset
from app.mlplatform.datasets.stats import DatasetStats, compute_stats

_EPS = 1e-9


class DriftSeverity(StrEnum):
    NONE = "none"
    MINOR = "minor"
    MODERATE = "moderate"
    SIGNIFICANT = "significant"

    @property
    def rank(self) -> int:
        return {"none": 0, "minor": 1, "moderate": 2, "significant": 3}[self.value]


def _severity_from_psi(psi: float) -> DriftSeverity:
    if psi < 0.1:
        return DriftSeverity.NONE if psi < 0.02 else DriftSeverity.MINOR
    if psi < 0.25:
        return DriftSeverity.MODERATE
    return DriftSeverity.SIGNIFICANT


# --------------------------------------------------------------------------- #
# Metric primitives
# --------------------------------------------------------------------------- #


def _normalize(counts: Mapping[str, int], keys: Sequence[str]) -> dict[str, float]:
    total = sum(counts.get(k, 0) for k in keys)
    if total == 0:
        return dict.fromkeys(keys, 0.0)
    return {k: counts.get(k, 0) / total for k in keys}


def psi(reference: Mapping[str, int], candidate: Mapping[str, int]) -> float:
    """Population Stability Index between two categorical distributions."""
    keys = sorted(set(reference) | set(candidate))
    if not keys:
        return 0.0
    ref = _normalize(reference, keys)
    cand = _normalize(candidate, keys)
    total = 0.0
    for k in keys:
        r = max(ref[k], _EPS)
        c = max(cand[k], _EPS)
        total += (c - r) * math.log(c / r)
    return round(total, 6)


def js_divergence(reference: Mapping[str, int], candidate: Mapping[str, int]) -> float:
    """Jensen-Shannon divergence (bits, 0..1) between two categorical dists."""
    keys = sorted(set(reference) | set(candidate))
    if not keys:
        return 0.0
    ref = _normalize(reference, keys)
    cand = _normalize(candidate, keys)

    def _kl(p: dict[str, float], q: dict[str, float]) -> float:
        out = 0.0
        for k in keys:
            pk = p[k]
            if pk <= 0:
                continue
            out += pk * math.log2(pk / max(q[k], _EPS))
        return out

    mid = {k: 0.5 * (ref[k] + cand[k]) for k in keys}
    return round(0.5 * _kl(ref, mid) + 0.5 * _kl(cand, mid), 6)


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sample Kolmogorov–Smirnov statistic (max empirical-CDF gap, 0..1)."""
    if not a or not b:
        return 0.0
    sa, sb = sorted(a), sorted(b)
    all_vals = sorted(set(sa) | set(sb))

    def _cdf(s: list[float], x: float) -> float:
        # fraction of s <= x
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi) // 2
            if s[mid] <= x:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(s)

    return round(max(abs(_cdf(sa, x) - _cdf(sb, x)) for x in all_vals), 6)


# --------------------------------------------------------------------------- #
# Per-feature + overall report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DriftMetric:
    """Drift on one feature, with the metric used and its severity."""

    feature: str
    metric: str
    value: float
    severity: DriftSeverity
    detail: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "metric": self.metric,
            "value": self.value,
            "severity": self.severity.value,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Per-feature drift + the overall (worst-feature) severity."""

    reference: str
    candidate: str
    metrics: tuple[DriftMetric, ...]
    overall: DriftSeverity

    @property
    def has_significant_drift(self) -> bool:
        return self.overall is DriftSeverity.SIGNIFICANT

    def worst(self) -> DriftMetric | None:
        return max(self.metrics, key=lambda m: m.severity.rank, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "candidate": self.candidate,
            "overall": self.overall.value,
            "has_significant_drift": self.has_significant_drift,
            "metrics": [m.to_dict() for m in self.metrics],
        }


_CATEGORICAL_FEATURES = ("role_dist", "task_dist", "label_dist", "model_dist", "split_dist")


def _numeric_severity(mean_shift: float, ks: float) -> DriftSeverity:
    score = max(mean_shift, ks)
    if score < 0.1:
        return DriftSeverity.NONE if score < 0.03 else DriftSeverity.MINOR
    if score < 0.25:
        return DriftSeverity.MODERATE
    return DriftSeverity.SIGNIFICANT


def drift_between_stats(
    reference: DatasetStats,
    candidate: DatasetStats,
    *,
    reward_ref: Sequence[float] = (),
    reward_cand: Sequence[float] = (),
) -> DriftReport:
    """Drift report from two precomputed stats snapshots (categorical features).

    Numeric KS needs the raw series; when available the caller passes the reward
    series, otherwise numeric drift uses the summary mean shift only.
    """
    metrics: list[DriftMetric] = []
    for feat in _CATEGORICAL_FEATURES:
        ref_counts = getattr(reference, feat)
        cand_counts = getattr(candidate, feat)
        p = psi(ref_counts, cand_counts)
        j = js_divergence(ref_counts, cand_counts)
        metrics.append(
            DriftMetric(
                feature=feat,
                metric="psi",
                value=p,
                severity=_severity_from_psi(p),
                detail={"js_divergence": j},
            )
        )

    # Reward: normalized mean shift (by reference stdev) + optional KS.
    ref_r, cand_r = reference.reward, candidate.reward
    denom = max(ref_r.stdev, 0.05)
    mean_shift = abs(cand_r.mean - ref_r.mean) / denom if ref_r.count else 0.0
    ks = ks_statistic(reward_ref, reward_cand) if reward_ref and reward_cand else 0.0
    metrics.append(
        DriftMetric(
            feature="reward",
            metric="mean_shift+ks",
            value=round(max(mean_shift, ks), 6),
            severity=_numeric_severity(mean_shift, ks),
            detail={"mean_shift": round(mean_shift, 6), "ks": ks},
        )
    )

    # Output length: normalized mean shift.
    ref_o, cand_o = reference.output_chars, candidate.output_chars
    o_denom = max(ref_o.stdev, 1.0)
    o_shift = abs(cand_o.mean - ref_o.mean) / o_denom if ref_o.count else 0.0
    metrics.append(
        DriftMetric(
            feature="output_chars",
            metric="mean_shift",
            value=round(o_shift, 6),
            severity=_numeric_severity(o_shift, 0.0),
            detail={"mean_shift": round(o_shift, 6)},
        )
    )

    overall = max((m.severity for m in metrics), key=lambda s: s.rank, default=DriftSeverity.NONE)
    return DriftReport(
        reference=reference.name,
        candidate=candidate.name,
        metrics=tuple(metrics),
        overall=overall,
    )


def drift_between(reference: Dataset, candidate: Dataset) -> DriftReport:
    """Drift report between two datasets (computes stats + passes reward series)."""
    return drift_between_stats(
        compute_stats(reference),
        compute_stats(candidate),
        reward_ref=[e.reward for e in reference.examples if e.reward is not None],
        reward_cand=[e.reward for e in candidate.examples if e.reward is not None],
    )


__all__ = [
    "DriftMetric",
    "DriftReport",
    "DriftSeverity",
    "drift_between",
    "drift_between_stats",
    "js_divergence",
    "ks_statistic",
    "psi",
]
