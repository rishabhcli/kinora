"""Domain telemetry facade — buffer health, render latency, QA, budget burn.

The four domain signals §13 / §12.5 single out — **buffer health**, **render
latency**, **QA pass-rate**, **budget burn** — are emitted from many hot paths
(the scheduler, the render pipeline, the critic). This module is the *one* import
those call sites reach for: each function fans out to both the low-level
observability Prometheus helper *and* the per-agent warehouse, so a single call
keeps Prometheus and the §13 warehouse in lock-step.

It is intentionally Prometheus-type-free and import-safe: the observability
helpers are imported lazily and guarded, so a call site can record a shot outcome
even in a context where the Prometheus registry was never set up (e.g. a focused
unit test), and the warehouse still updates.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.telemetry.warehouse import MetricsWarehouse, get_warehouse

logger = get_logger("app.telemetry.domain")


def _obs() -> object | None:
    """Return the observability metrics module, or ``None`` if unavailable."""
    try:
        from app.observability import metrics as _m

        return _m
    except Exception:  # noqa: BLE001 - facade must work without the registry
        return None


# --------------------------------------------------------------------------- #
# Render latency + mode.
# --------------------------------------------------------------------------- #


def record_render_latency(mode: str, seconds: float) -> None:
    """Record one per-shot render latency (Prometheus histogram, by mode/rung)."""
    obs = _obs()
    if obs is not None:
        obs.observe_render_latency(mode, seconds)  # type: ignore[attr-defined]


def record_render_mode(mode: str) -> None:
    """Count one shot rendered with a given Wan render mode (§9.3 distribution)."""
    obs = _obs()
    if obs is not None:
        obs.inc_render_mode(mode)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# QA pass-rate (Critic verdict) → both Prometheus and the warehouse.
# --------------------------------------------------------------------------- #


def record_qa(
    *,
    ccs: float | None = None,
    style_drift: float | None = None,
    motion: float | None = None,
    agent: str = "generator",
    warehouse: MetricsWarehouse | None = None,
) -> None:
    """Record Critic QA scores into Prometheus *and* the per-agent warehouse."""
    obs = _obs()
    if obs is not None:
        obs.observe_qa(ccs=ccs, style_drift=style_drift, motion=motion)  # type: ignore[attr-defined]
    (warehouse or get_warehouse()).record_qa(agent, ccs=ccs, style_drift=style_drift, motion=motion)


def record_shot_outcome(
    *,
    accepted: bool,
    regenerations: int = 0,
    video_seconds: float = 0.0,
    agent: str = "generator",
    warehouse: MetricsWarehouse | None = None,
) -> None:
    """Record a terminal shot outcome (accepted vs degraded) everywhere at once."""
    obs = _obs()
    if obs is not None:
        if accepted:
            obs.inc_shot_accepted()  # type: ignore[attr-defined]
        else:
            obs.inc_shot_degraded()  # type: ignore[attr-defined]
        if regenerations > 0:
            obs.inc_render_retries(regenerations)  # type: ignore[attr-defined]
        if video_seconds > 0:
            obs.inc_video_seconds(video_seconds)  # type: ignore[attr-defined]
    (warehouse or get_warehouse()).record_shot_outcome(
        agent,
        accepted=accepted,
        regenerations=regenerations,
        video_seconds=video_seconds,
    )


# --------------------------------------------------------------------------- #
# Buffer health (the §4.10 sawtooth + watermark crossings).
# --------------------------------------------------------------------------- #


def record_buffer_occupancy(session: str, seconds: float) -> None:
    """Set a session's committed-buffer occupancy gauge (bounded LRU)."""
    obs = _obs()
    if obs is not None:
        obs.set_buffer_occupancy(session, seconds)  # type: ignore[attr-defined]


def record_watermark_crossing(direction: str) -> None:
    """Count a dual-watermark crossing (``low`` starts a burst, ``high`` stops)."""
    obs = _obs()
    if obs is not None:
        obs.inc_watermark_crossing(direction)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Budget burn (video-seconds — the budget-critical resource, §11.1).
# --------------------------------------------------------------------------- #


def record_budget_burn(video_seconds: float) -> None:
    """Add spent Wan video-seconds to the running budget-burn total."""
    obs = _obs()
    if obs is not None:
        obs.inc_video_seconds(video_seconds)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Conflicts (§7.2) — both the surfaced count and the resolution policy.
# --------------------------------------------------------------------------- #


def record_conflict() -> None:
    """Count one continuity conflict surfaced to the director (§7.2)."""
    obs = _obs()
    if obs is not None:
        obs.inc_conflict()  # type: ignore[attr-defined]


def record_conflict_resolved(option: str) -> None:
    """Count one §7.2 conflict resolution by the chosen policy option."""
    obs = _obs()
    if obs is not None:
        obs.inc_conflict_resolved(option)  # type: ignore[attr-defined]


def publish_warehouse_to_prometheus(warehouse: MetricsWarehouse | None = None) -> None:
    """Mirror the warehouse rollup into the per-agent Prometheus gauges."""
    (warehouse or get_warehouse()).export_prometheus()


__all__ = [
    "publish_warehouse_to_prometheus",
    "record_budget_burn",
    "record_buffer_occupancy",
    "record_conflict",
    "record_conflict_resolved",
    "record_qa",
    "record_render_latency",
    "record_render_mode",
    "record_shot_outcome",
    "record_watermark_crossing",
]
