"""Service-level objectives + multi-window burn-rate alerting math.

An SLO pins a target (e.g. "99.5% of API requests succeed") over a rolling
window. The complement of the target is the **error budget** (0.5%); spending it
faster than the window allows is a **burn rate** > 1. The Google SRE workbook's
multi-window/multi-burn-rate alerting fires when a *short* window and a *long*
window are both burning fast — catching real incidents while ignoring blips.

This module is pure math + data: it defines Kinora's SLO set (mapped onto the §13
/ §12.5 signals), computes burn rate from an observed bad-event ratio, and yields
the alert thresholds the rule generator (:mod:`app.telemetry.alerts`) renders to
Prometheus. It performs no I/O and reads no live metrics — the values are
supplied by whoever scrapes Prometheus (or by a test), which keeps it trivially
unit-testable and offline-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Seconds per common rolling window (used to convert windows ↔ a 30-day budget).
WINDOW_30D_S = 30 * 24 * 3600


class SLOKind(StrEnum):
    """Whether the SLI is an availability ratio or a latency threshold."""

    AVAILABILITY = "availability"
    LATENCY = "latency"
    QUALITY = "quality"


@dataclass(frozen=True, slots=True)
class BurnRateWindow:
    """One multi-window burn-rate alert tier (SRE-workbook style).

    A tier fires when the burn rate over *both* a long window and a (1/12) short
    window exceeds ``burn_rate``. Faster burn → shorter windows → higher urgency.
    """

    name: str
    long_window: str  # Prometheus duration, e.g. "1h"
    short_window: str  # Prometheus duration, e.g. "5m"
    burn_rate: float
    severity: str  # "page" | "ticket"

    def budget_consumed_fraction(self, window_30d_s: float = WINDOW_30D_S) -> float:
        """Fraction of a 30-day error budget consumed if this rate held the window."""
        long_s = parse_duration(self.long_window)
        return self.burn_rate * (long_s / window_30d_s)


@dataclass(frozen=True, slots=True)
class SLO:
    """A single service-level objective."""

    name: str
    description: str
    kind: SLOKind
    objective: float  # the target, e.g. 0.995 (avail) or 0.99 (latency under threshold)
    window: str = "30d"
    sli_query: str = ""  # the Prometheus ratio query for the *good*-event fraction
    latency_threshold_s: float | None = None
    burn_windows: tuple[BurnRateWindow, ...] = ()

    @property
    def error_budget(self) -> float:
        """The allowed bad-event fraction (1 − objective)."""
        return max(0.0, 1.0 - self.objective)

    def burn_rate(self, bad_ratio: float) -> float:
        """Burn rate for an observed bad-event ratio (bad/total).

        ``1.0`` means the budget is being spent exactly at the sustainable rate;
        ``> 1`` means faster (an incident if sustained). Returns ``inf`` when the
        objective is a perfect ``1.0`` and any bad events are seen.
        """
        budget = self.error_budget
        if budget <= 0.0:
            return float("inf") if bad_ratio > 0 else 0.0
        return max(0.0, bad_ratio) / budget

    def budget_remaining(self, bad_ratio: float) -> float:
        """Fraction of the error budget still unspent (clamped to ``[0, 1]``)."""
        budget = self.error_budget
        if budget <= 0.0:
            return 0.0 if bad_ratio > 0 else 1.0
        return max(0.0, min(1.0, 1.0 - bad_ratio / budget))

    def is_breaching(self, good_ratio: float) -> bool:
        """True when the observed *good* ratio is below the objective."""
        return good_ratio < self.objective


def parse_duration(value: str) -> float:
    """Parse a Prometheus-style duration (``"5m"``, ``"1h"``, ``"30d"``) → seconds."""
    value = value.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if not value or value[-1] not in units:
        raise ValueError(f"unrecognized duration {value!r}")
    return float(value[:-1]) * units[value[-1]]


# --------------------------------------------------------------------------- #
# The standard multi-window burn-rate tiers (SRE workbook defaults).
# --------------------------------------------------------------------------- #


def standard_burn_windows() -> tuple[BurnRateWindow, ...]:
    """The four-tier multi-window burn-rate ladder (page fast, ticket slow)."""
    return (
        BurnRateWindow(
            "fast", long_window="1h", short_window="5m", burn_rate=14.4, severity="page"
        ),
        BurnRateWindow("mid", long_window="6h", short_window="30m", burn_rate=6.0, severity="page"),
        BurnRateWindow(
            "slow", long_window="1d", short_window="2h", burn_rate=3.0, severity="ticket"
        ),
        BurnRateWindow(
            "trickle", long_window="3d", short_window="6h", burn_rate=1.0, severity="ticket"
        ),
    )


# --------------------------------------------------------------------------- #
# Kinora's SLO catalogue (mapped onto §13 / §12.5 signals).
# --------------------------------------------------------------------------- #


def default_slos() -> tuple[SLO, ...]:
    """The Kinora SLO set tied to the live Prometheus series.

    The ``sli_query`` strings reference the existing observability metric names
    (``kinora_http_requests_total`` etc.) so the generated recording/alerting
    rules drop straight into a Prometheus that scrapes ``/metrics``.
    """
    burn = standard_burn_windows()
    return (
        SLO(
            name="api_availability",
            description="Fraction of API requests that did not 5xx (RED: errors).",
            kind=SLOKind.AVAILABILITY,
            objective=0.995,
            sli_query=(
                'sum(rate(kinora_http_requests_total{status!~"5.."}[5m])) '
                "/ sum(rate(kinora_http_requests_total[5m]))"
            ),
            burn_windows=burn,
        ),
        SLO(
            name="api_latency",
            description="Fraction of API requests served under 1s (RED: duration).",
            kind=SLOKind.LATENCY,
            objective=0.99,
            latency_threshold_s=1.0,
            sli_query=(
                'sum(rate(kinora_http_request_duration_seconds_bucket{le="1.0"}[5m])) '
                "/ sum(rate(kinora_http_request_duration_seconds_count[5m]))"
            ),
            burn_windows=burn,
        ),
        SLO(
            name="render_job_success",
            description="Fraction of render jobs that succeeded (USE: errors).",
            kind=SLOKind.AVAILABILITY,
            objective=0.97,
            sli_query=(
                'sum(rate(kinora_jobs_total{status="succeeded"}[15m])) '
                '/ sum(rate(kinora_jobs_total{status=~"succeeded|deadletter"}[15m]))'
            ),
            burn_windows=burn,
        ),
        SLO(
            name="buffer_health",
            description="Reading time the committed buffer stayed above L (§13, target >99%).",
            kind=SLOKind.QUALITY,
            objective=0.99,
            sli_query=(
                "avg_over_time(kinora_buffer_occupancy_seconds[5m]) "
                "> bool 0"  # placeholder ratio; the real SLI is computed in the warehouse
            ),
            burn_windows=burn,
        ),
        SLO(
            name="qa_pass_rate",
            description="Fraction of shots accepted at full footage (§13 acceptance).",
            kind=SLOKind.QUALITY,
            objective=0.9,
            sli_query=(
                "sum(rate(kinora_shots_accepted_total[1h])) "
                "/ (sum(rate(kinora_shots_accepted_total[1h])) "
                "+ sum(rate(kinora_shots_degraded_total[1h])))"
            ),
            burn_windows=burn,
        ),
        SLO(
            name="ccs_quality",
            description="Mean Character Consistency Score stays at/above the §9.5 floor 0.85.",
            kind=SLOKind.QUALITY,
            objective=0.85,
            sli_query='histogram_quantile(0.5, rate(kinora_qa_score_bucket{metric="ccs"}[1h]))',
            burn_windows=(),  # a quality floor, not an error-budget burn
        ),
    )


@dataclass(frozen=True, slots=True)
class SLOEvaluation:
    """The evaluated state of one SLO against an observed good ratio."""

    slo: str
    objective: float
    good_ratio: float
    bad_ratio: float = field(init=False)
    burn_rate: float = field(init=False)
    budget_remaining: float = field(init=False)
    breaching: bool = field(init=False)

    def __post_init__(self) -> None:
        bad = max(0.0, 1.0 - self.good_ratio)
        object.__setattr__(self, "bad_ratio", bad)

    @classmethod
    def evaluate(cls, slo: SLO, good_ratio: float) -> SLOEvaluation:
        """Evaluate an SLO against an observed good-event ratio."""
        ev = cls(slo=slo.name, objective=slo.objective, good_ratio=good_ratio)
        bad = max(0.0, 1.0 - good_ratio)
        object.__setattr__(ev, "burn_rate", slo.burn_rate(bad))
        object.__setattr__(ev, "budget_remaining", slo.budget_remaining(bad))
        object.__setattr__(ev, "breaching", slo.is_breaching(good_ratio))
        return ev

    def to_dict(self) -> dict[str, float | str | bool]:
        burn = self.burn_rate
        return {
            "slo": self.slo,
            "objective": round(self.objective, 6),
            "good_ratio": round(self.good_ratio, 6),
            "bad_ratio": round(self.bad_ratio, 6),
            "burn_rate": ("inf" if burn == float("inf") else round(burn, 4)),
            "budget_remaining": round(self.budget_remaining, 6),
            "breaching": self.breaching,
        }


def slo_catalogue() -> dict[str, object]:
    """Return the SLO set as a JSON-safe catalogue (for the read endpoint)."""
    out = []
    for slo in default_slos():
        out.append(
            {
                "name": slo.name,
                "description": slo.description,
                "kind": str(slo.kind),
                "objective": slo.objective,
                "window": slo.window,
                "error_budget": round(slo.error_budget, 6),
                "latency_threshold_s": slo.latency_threshold_s,
                "sli_query": slo.sli_query,
                "burn_windows": [
                    {
                        "name": w.name,
                        "long_window": w.long_window,
                        "short_window": w.short_window,
                        "burn_rate": w.burn_rate,
                        "severity": w.severity,
                    }
                    for w in slo.burn_windows
                ],
            }
        )
    return {"slos": out}


__all__ = [
    "WINDOW_30D_S",
    "BurnRateWindow",
    "SLO",
    "SLOEvaluation",
    "SLOKind",
    "default_slos",
    "parse_duration",
    "slo_catalogue",
    "standard_burn_windows",
]
