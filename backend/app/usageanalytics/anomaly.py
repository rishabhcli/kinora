"""Anomaly detection over the metric series → operator alerts (kinora.md §11).

Pure detectors over a dense series of bucketed cells. Each detector compares a
*recent* window against a rolling *baseline* and emits an :class:`Alert` when the
deviation crosses a configured threshold. Three detectors ship:

* **spend spike** — the latest bucket's USD cost is materially above the trailing
  baseline mean (robust z-score / ratio); catches a runaway render or a pricing
  mistake before the cap does.
* **error-rate surge** — the recent error-rate jumps above the baseline by an
  absolute margin (with a minimum call volume so a 1/1 failure on a quiet bucket
  doesn't page anyone).
* **quality regression** — the recent mean quality drops below the baseline mean
  by an absolute margin (the crew started producing worse footage).

Everything is pure: detectors take already-aggregated series and return alerts;
no I/O, no clock, no randomness. The detector config has sensible defaults and is
overridable from settings.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from statistics import StatisticsError, mean, pstdev
from typing import Any

from app.usageanalytics.events import MetricCell


class AnomalyKind(enum.StrEnum):
    """The kind of anomaly an alert reports."""

    SPEND_SPIKE = "spend_spike"
    ERROR_SURGE = "error_surge"
    QUALITY_REGRESSION = "quality_regression"


class Severity(enum.IntEnum):
    """Monotone alert severity (compare/escalate with ``>=``)."""

    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class Alert:
    """One detected anomaly, ready to render or forward to the notify bridge."""

    kind: AnomalyKind
    severity: Severity
    at: datetime
    observed: float
    baseline: float
    threshold: float
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.label,
            "at": self.at.isoformat(),
            "observed": round(self.observed, 6),
            "baseline": round(self.baseline, 6),
            "threshold": round(self.threshold, 6),
            "message": self.message,
            "context": self.context,
        }


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Thresholds the detectors trip on (all overridable from settings)."""

    #: Minimum trailing buckets needed before a baseline is trustworthy.
    min_baseline_buckets: int = 3
    #: Spend spike: latest cost must exceed ``baseline_mean * spike_ratio``.
    spend_spike_ratio: float = 3.0
    #: Spend spike: and exceed the baseline mean by at least this many σ.
    spend_spike_sigma: float = 3.0
    #: Spend spike: absolute floor (USD) below which we never alert (noise).
    spend_spike_min_usd: float = 0.50
    #: Error surge: absolute rise in error-rate over baseline that trips WARNING.
    error_surge_delta: float = 0.10
    #: Error surge: absolute error-rate that trips CRITICAL regardless of baseline.
    error_surge_critical: float = 0.50
    #: Error surge: minimum calls in the recent bucket to consider it.
    error_surge_min_calls: int = 20
    #: Quality regression: absolute drop in mean quality over baseline.
    quality_drop_delta: float = 0.08
    #: Quality regression: minimum quality-scored calls in recent bucket.
    quality_min_samples: int = 10


@dataclass(frozen=True, slots=True)
class _Point:
    """A bucketed cell with its start, the unit the detectors consume."""

    at: datetime
    cell: MetricCell


def _points(buckets: dict[datetime, MetricCell]) -> list[_Point]:
    return [_Point(at, cell) for at, cell in sorted(buckets.items())]


def _safe_pstdev(xs: list[float]) -> float:
    try:
        return pstdev(xs) if len(xs) >= 2 else 0.0
    except StatisticsError:  # pragma: no cover - guarded above
        return 0.0


def detect_spend_spike(
    buckets: dict[datetime, MetricCell], cfg: DetectorConfig = DetectorConfig()
) -> Alert | None:
    """Flag the latest bucket if its USD cost spikes above the trailing baseline.

    Trips when the latest cost is both ``>= spike_ratio × baseline_mean`` **and**
    ``>= baseline_mean + spike_sigma × σ``, and above the absolute noise floor.
    Severity escalates to CRITICAL at ``2 × spike_ratio``.
    """
    pts = _points(buckets)
    if len(pts) < cfg.min_baseline_buckets + 1:
        return None
    latest = pts[-1]
    baseline = [float(p.cell.cost_usd) for p in pts[:-1]]
    observed = float(latest.cell.cost_usd)
    if observed < cfg.spend_spike_min_usd:
        return None
    base_mean = mean(baseline) if baseline else 0.0
    sigma = _safe_pstdev(baseline)
    ratio_ok = observed >= cfg.spend_spike_ratio * base_mean if base_mean > 0 else True
    sigma_ok = observed >= base_mean + cfg.spend_spike_sigma * sigma if sigma > 0 else True
    if not (ratio_ok and sigma_ok):
        return None
    severity = Severity.WARNING
    if base_mean > 0 and observed >= 2.0 * cfg.spend_spike_ratio * base_mean:
        severity = Severity.CRITICAL
    return Alert(
        kind=AnomalyKind.SPEND_SPIKE,
        severity=severity,
        at=latest.at,
        observed=observed,
        baseline=base_mean,
        threshold=cfg.spend_spike_ratio * base_mean if base_mean > 0 else cfg.spend_spike_min_usd,
        message=(
            f"spend spike: ${observed:.4f} vs baseline ${base_mean:.4f} "
            f"(>{cfg.spend_spike_ratio}x)"
        ),
        context={"sigma": round(sigma, 6), "baseline_buckets": len(baseline)},
    )


def detect_error_surge(
    buckets: dict[datetime, MetricCell], cfg: DetectorConfig = DetectorConfig()
) -> Alert | None:
    """Flag the latest bucket if its error-rate surges over the baseline.

    Requires ``error_surge_min_calls`` in the latest bucket. Trips WARNING when the
    error-rate rises by ``error_surge_delta`` over the baseline mean, and CRITICAL
    when the absolute error-rate is at/above ``error_surge_critical``.
    """
    pts = _points(buckets)
    if len(pts) < cfg.min_baseline_buckets + 1:
        return None
    latest = pts[-1]
    if latest.cell.calls < cfg.error_surge_min_calls:
        return None
    observed = latest.cell.error_rate
    base_mean = mean([p.cell.error_rate for p in pts[:-1]])
    crit = observed >= cfg.error_surge_critical
    surge = observed >= base_mean + cfg.error_surge_delta
    if not (crit or surge):
        return None
    return Alert(
        kind=AnomalyKind.ERROR_SURGE,
        severity=Severity.CRITICAL if crit else Severity.WARNING,
        at=latest.at,
        observed=observed,
        baseline=base_mean,
        threshold=cfg.error_surge_critical if crit else base_mean + cfg.error_surge_delta,
        message=(
            f"error-rate surge: {observed:.1%} vs baseline {base_mean:.1%} "
            f"on {latest.cell.calls} calls"
        ),
        context={"errors": latest.cell.errors, "calls": latest.cell.calls},
    )


def detect_quality_regression(
    buckets: dict[datetime, MetricCell], cfg: DetectorConfig = DetectorConfig()
) -> Alert | None:
    """Flag the latest bucket if mean quality drops below the baseline.

    Only considers buckets that carry a quality signal; requires
    ``quality_min_samples`` quality-scored calls in the latest bucket. Trips when
    the recent mean quality falls by ``quality_drop_delta`` under the baseline.
    """
    pts = [p for p in _points(buckets) if p.cell.avg_quality is not None]
    if len(pts) < cfg.min_baseline_buckets + 1:
        return None
    latest = pts[-1]
    if latest.cell.quality_count < cfg.quality_min_samples:
        return None
    observed = latest.cell.avg_quality
    assert observed is not None  # guarded by the comprehension
    base_vals = [p.cell.avg_quality for p in pts[:-1] if p.cell.avg_quality is not None]
    if not base_vals:
        return None
    base_mean = mean(base_vals)
    drop = base_mean - observed
    if drop < cfg.quality_drop_delta:
        return None
    severity = Severity.CRITICAL if drop >= 2 * cfg.quality_drop_delta else Severity.WARNING
    return Alert(
        kind=AnomalyKind.QUALITY_REGRESSION,
        severity=severity,
        at=latest.at,
        observed=observed,
        baseline=base_mean,
        threshold=base_mean - cfg.quality_drop_delta,
        message=(
            f"quality regression: {observed:.3f} vs baseline {base_mean:.3f} "
            f"(drop {drop:.3f})"
        ),
        context={"samples": latest.cell.quality_count},
    )


def detect_all(
    buckets: dict[datetime, MetricCell], cfg: DetectorConfig = DetectorConfig()
) -> list[Alert]:
    """Run every detector over a series; return the alerts that fired (severity desc)."""
    alerts = [
        detect_spend_spike(buckets, cfg),
        detect_error_surge(buckets, cfg),
        detect_quality_regression(buckets, cfg),
    ]
    fired = [a for a in alerts if a is not None]
    return sorted(fired, key=lambda a: a.severity, reverse=True)


__all__ = [
    "Alert",
    "AnomalyKind",
    "DetectorConfig",
    "Severity",
    "detect_all",
    "detect_error_surge",
    "detect_quality_regression",
    "detect_spend_spike",
]
