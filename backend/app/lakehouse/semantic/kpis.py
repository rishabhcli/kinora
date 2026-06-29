"""The §13 Kinora KPIs, defined as code in the metrics language.

kinora.md §13 ("Metrics & the eval harness") names the numbers a judge watches.
The pure math already lives in :mod:`app.eval.metrics`; this module re-expresses
the *aggregatable* KPIs declaratively so they can be sliced by book / agent-role
/ time-grain through the self-serve query API — the online, dashboard-shaped
counterpart to the offline eval harness.

Modelled on the :func:`app.lakehouse.semantic.tests`-shaped render-telemetry
star schema (a ``shots`` fact + a ``buffer`` fact), the KPIs are:

* **accepted_footage_efficiency** — ``(1 − rejected/total) × 100`` (the headline
  budget number, §13). A derived metric over two simple measures.
* **regen_rate** — ``regens / total_shots`` (lower is better). A ratio metric.
* **ccs** — the *mean* per-shot Character Consistency Score, as
  ``ccs_sum / shot_count`` (the online proxy for §13's embedding CCS; the true
  embedding cosine is computed upstream and summed into ``ccs_sum``).
* **budget_burn** — cumulative USD spent (running total over time); plus
  ``budget_burn_rate`` as the per-grain spend and its period-over-period change.
* **buffer_health** — ``above_low_count / sample_count`` (fraction of reading
  time the committed buffer stayed at/above the low watermark ``L``; target
  > 99%, §13) and **buffer_stalls** (visible stalls; target 0).

These return a list of model + metric definitions ready to hand to
:meth:`SemanticGraph.build`; the catalog tags them so they surface as the
"Kinora KPIs" group.
"""

from __future__ import annotations

from app.lakehouse.semantic.metrics import (
    CumulativeMetric,
    DerivedMetric,
    Metric,
    RatioMetric,
    SimpleMetric,
    TimeComparisonMetric,
    WindowKind,
)

#: A presentation tag the catalog groups KPIs under.
KPI_GROUP = "kinora_kpis"


def kpi_metrics() -> tuple[Metric, ...]:
    """Return the §13 KPI metric definitions (metrics-as-code)."""
    return (
        # -- base simple metrics (the measure leaves) -------------------- #
        SimpleMetric(
            name="total_video_seconds",
            measure="total_seconds",
            label="Total Video Seconds",
            description="All generated video-seconds (accepted + rejected).",
        ),
        SimpleMetric(
            name="rejected_video_seconds",
            measure="rejected_seconds",
            label="Rejected Video Seconds",
            description="Video-seconds that failed QA (§9.5).",
        ),
        SimpleMetric(
            name="shot_total",
            measure="shot_count",
            label="Shots",
        ),
        SimpleMetric(
            name="regens_total",
            measure="regen_count",
            label="Regenerations",
        ),
        SimpleMetric(
            name="ccs_total",
            measure="ccs_sum",
            label="CCS Sum",
        ),
        SimpleMetric(
            name="usd_total",
            measure="usd_spent",
            label="USD Spent",
            format="usd",
        ),
        # -- the headline KPIs ------------------------------------------ #
        DerivedMetric(
            name="accepted_footage_efficiency",
            expr="(1 - rejected / total) * 100",
            inputs={"rejected": "rejected_video_seconds", "total": "total_video_seconds"},
            label="Accepted-Footage Efficiency",
            description="(1 − rejected/total) × 100 — QA-passed video per 100s of budget (§13).",
            format="percent",
        ),
        RatioMetric(
            name="regen_rate",
            numerator="regens_total",
            denominator="shot_total",
            label="Regeneration Rate",
            description="regens / total_shots — lower is better (§13).",
        ),
        RatioMetric(
            name="ccs",
            numerator="ccs_total",
            denominator="shot_total",
            label="Character Consistency Score",
            description="Mean per-shot CCS (§13 online proxy).",
        ),
        # -- budget burn (cumulative + rate-of-change) ------------------ #
        CumulativeMetric(
            name="budget_burn",
            base="usd_total",
            window=WindowKind.ALL_TIME,
            label="Budget Burn (cumulative USD)",
            description="Running total of USD spent over the reading session (§11.1).",
            format="usd",
        ),
        TimeComparisonMetric(
            name="budget_burn_change",
            base="usd_total",
            offset_periods=1,
            label="Budget Burn — period over period (%)",
            description="Period-over-period change in spend (§11.1 budget pressure).",
            format="percent",
        ),
    )


def buffer_kpi_metrics() -> tuple[Metric, ...]:
    """Buffer-health KPIs (modelled on the separate ``buffer`` fact, §5.3/§13)."""
    return (
        SimpleMetric(
            name="buffer_samples",
            measure="sample_count",
            label="Buffer Samples",
        ),
        SimpleMetric(
            name="buffer_above_low",
            measure="above_low_count",
            label="Samples Above L",
        ),
        SimpleMetric(
            name="buffer_stalls",
            measure="stall_count",
            label="Buffer Stalls",
            description="Count of visible stalls (committed buffer hit 0); target 0 (§13).",
        ),
        RatioMetric(
            name="buffer_health",
            numerator="buffer_above_low",
            denominator="buffer_samples",
            label="Buffer Health",
            description="Fraction of reading time the buffer stayed >= L; >0.99 target (§13).",
            format="percent",
        ),
    )


#: Catalog descriptions keyed by metric name (the curated KPI catalog copy).
KPI_CATALOG_TAGS: dict[str, tuple[str, ...]] = {
    "accepted_footage_efficiency": (KPI_GROUP, "budget", "headline"),
    "regen_rate": (KPI_GROUP, "quality"),
    "ccs": (KPI_GROUP, "consistency", "headline"),
    "budget_burn": (KPI_GROUP, "budget"),
    "budget_burn_change": (KPI_GROUP, "budget"),
    "buffer_health": (KPI_GROUP, "buffer", "headline"),
    "buffer_stalls": (KPI_GROUP, "buffer"),
}


__all__ = [
    "KPI_CATALOG_TAGS",
    "KPI_GROUP",
    "buffer_kpi_metrics",
    "kpi_metrics",
]
