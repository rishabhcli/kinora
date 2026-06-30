"""Human-readable SLO + health report rendering (the ops/CLI text view).

Pure formatters over the engine's :class:`~app.slo.engine.SLOStatus` and the
health framework's :class:`~app.slo.health.HealthReport`. No state — the API
surface and a future CLI both render the same text.
"""

from __future__ import annotations

from app.slo.engine import SLOStatus
from app.slo.health import HealthReport


def render_status_text(status: SLOStatus) -> str:
    """A compact multi-line SLO summary (gate, budgets, alerts, latency)."""
    lines: list[str] = []
    g = status.gate
    overall = "HEALTHY" if status.healthy else "DEGRADED"
    lines.append(f"SLO status: {overall}  gate={g.decision.value.upper()}")
    lines.append(
        f"  release={'yes' if g.can_release else 'NO'} "
        f"canary={'yes' if g.can_promote_canary else 'NO'} "
        f"min_budget={g.min_budget_remaining * 100:.1f}%"
    )
    for reason in g.reasons:
        lines.append(f"    - {reason}")

    lines.append("  error budgets:")
    for b in status.budgets:
        rem = b.remaining_fraction
        mark = "ok " if b.met and not b.is_exhausted else "LOW" if not b.is_exhausted else "OUT"
        rem_s = "blown" if rem < 0 else f"{rem * 100:.1f}%"
        lines.append(
            f"    [{mark}] {b.objective.name:<22} good={b.good_ratio * 100:.3f}% "
            f"target={b.objective.target * 100:.2f}% remaining={rem_s} (n={b.sample_count})"
        )

    if status.latency:
        lines.append("  latency objectives:")
        for v in status.latency:
            mark = "ok " if v.met else "MISS"
            lines.append(
                f"    [{mark}] {v.objective.name:<22} "
                f"measured={v.measured_ms:.1f}ms target={v.objective.target_ms:.1f}ms"
            )

    firing = status.firing_alerts
    if firing:
        lines.append("  burn alerts FIRING:")
        for a in firing:
            lines.append(f"    [{a.severity.value.upper()}] {a.objective_name}: {a.detail}")
    else:
        lines.append("  burn alerts: none firing")
    return "\n".join(lines)


def render_health_text(report: HealthReport) -> str:
    """A compact health summary (aggregate + per-dependency)."""
    lines = [
        f"health: {report.status.value.upper()} "
        f"ready={'yes' if report.ready else 'NO'} ({report.duration_ms:.1f}ms)"
    ]
    for o in report.outcomes:
        detail = f" — {o.result.detail}" if o.result.detail else ""
        lines.append(
            f"  [{o.status.value:<8}] {o.name:<16} "
            f"({o.probe.criticality.value}, {o.latency_ms:.1f}ms){detail}"
        )
    return "\n".join(lines)


__all__ = ["render_health_text", "render_status_text"]
