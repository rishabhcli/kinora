"""Prometheus mirror for the per-agent warehouse rollup (§13 → /metrics).

The observability package owns the Prometheus ``registry`` and the per-shot /
per-session series. This module adds the **per-agent** warehouse series on the
*same* registry so they appear on the existing ``/metrics`` endpoint, keyed only
by the bounded crew-role label (no session/shot label → bounded cardinality).

Gauges (not counters) are used: the warehouse already holds the running totals,
so on each publish we ``set`` the gauge to the current value. Registration is
done once and is import-safe — if the Prometheus registry is unavailable the
module degrades to a no-op set of stubs and ``publish_warehouse`` does nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.telemetry.warehouse import AgentStats

logger = get_logger("app.telemetry.promstore")

_AGENT_LABEL = "agent"
_registered = False
_gauges: dict[str, Any] = {}


def _register() -> bool:
    """Register the per-agent gauges on the shared registry (idempotent)."""
    global _registered
    if _registered:
        return bool(_gauges)
    _registered = True
    try:
        from prometheus_client import Gauge

        from app.observability.metrics import registry
    except Exception as exc:  # noqa: BLE001 - mirroring is optional
        logger.debug("telemetry.promstore_unavailable", error=str(exc))
        return False

    def gauge(name: str, doc: str) -> Any:
        return Gauge(name, doc, labelnames=(_AGENT_LABEL,), registry=registry)

    _gauges.update(
        {
            "calls": gauge("kinora_agent_calls_total_gauge", "Crew calls per agent role."),
            "errors": gauge("kinora_agent_errors_gauge", "Crew errored calls per agent role."),
            "repairs": gauge(
                "kinora_agent_repairs_gauge", "JSON-repair round-trips per agent role."
            ),
            "input_tokens": gauge(
                "kinora_agent_input_tokens_gauge", "Input tokens billed per agent role."
            ),
            "output_tokens": gauge(
                "kinora_agent_output_tokens_gauge", "Output tokens billed per agent role."
            ),
            "cost_usd": gauge("kinora_agent_cost_usd_gauge", "Estimated USD spend per agent role."),
            "latency_mean_s": gauge(
                "kinora_agent_latency_mean_seconds", "Mean call latency per agent role."
            ),
            "latency_p95_s": gauge(
                "kinora_agent_latency_p95_seconds", "p95 call latency per agent role."
            ),
            "mean_ccs": gauge("kinora_agent_mean_ccs_gauge", "Mean CCS attributed per agent role."),
            "acceptance_rate": gauge(
                "kinora_agent_acceptance_rate_gauge",
                "Accepted/(accepted+degraded) per agent role.",
            ),
            "video_seconds": gauge(
                "kinora_agent_video_seconds_gauge", "Video-seconds attributed per agent role."
            ),
        }
    )
    return True


def publish_warehouse(agents: Iterable[AgentStats]) -> None:
    """Set the per-agent gauges from a warehouse snapshot (best-effort)."""
    if not _register():
        return
    try:
        for s in agents:
            label = s.role
            _gauges["calls"].labels(**{_AGENT_LABEL: label}).set(s.calls)
            _gauges["errors"].labels(**{_AGENT_LABEL: label}).set(s.errors)
            _gauges["repairs"].labels(**{_AGENT_LABEL: label}).set(s.repairs)
            _gauges["input_tokens"].labels(**{_AGENT_LABEL: label}).set(s.input_tokens)
            _gauges["output_tokens"].labels(**{_AGENT_LABEL: label}).set(s.output_tokens)
            _gauges["cost_usd"].labels(**{_AGENT_LABEL: label}).set(s.cost_usd)
            _gauges["latency_mean_s"].labels(**{_AGENT_LABEL: label}).set(s.mean_latency_s)
            _gauges["latency_p95_s"].labels(**{_AGENT_LABEL: label}).set(s.latency.percentile(0.95))
            _gauges["mean_ccs"].labels(**{_AGENT_LABEL: label}).set(s.mean_ccs or 0.0)
            _gauges["acceptance_rate"].labels(**{_AGENT_LABEL: label}).set(s.acceptance_rate or 0.0)
            _gauges["video_seconds"].labels(**{_AGENT_LABEL: label}).set(s.video_seconds)
    except Exception as exc:  # noqa: BLE001 - never break a publish
        logger.debug("telemetry.promstore_publish_failed", error=str(exc))


__all__ = ["publish_warehouse"]
