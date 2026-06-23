"""Prometheus metrics — the §12.5 per-shot + per-session telemetry surface.

Uses a dedicated :class:`~prometheus_client.CollectorRegistry` (not the global
default) so metrics are isolated and re-importing the module during tests cannot
trigger duplicate-registration errors.

Two design rules keep this safe to call from every hot path:

* **One-liner emit helpers.** Call sites import a tiny typed function
  (``observe_render``/``inc_cache``/``set_buffer_occupancy``/``observe_provider``
  …) so instrumentation never leaks Prometheus types into business logic.
* **Bounded cardinality.** Counters that would otherwise carry a per-session
  label are kept *aggregate* (no ``session`` label); the only session-labelled
  series is the buffer-occupancy gauge, which is capped to a bounded LRU set and
  cleared on session end via :func:`clear_session_metrics`.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: Process-wide registry that all Kinora metrics register against.
registry: CollectorRegistry = CollectorRegistry(auto_describe=True)

# --------------------------------------------------------------------------- #
# HTTP / app info (Phase 9)
# --------------------------------------------------------------------------- #

http_requests_total: Counter = Counter(
    "kinora_http_requests_total",
    "Total number of HTTP requests handled, labelled by method/path/status.",
    labelnames=("method", "path", "status"),
    registry=registry,
)

app_info: Gauge = Gauge(
    "kinora_app_info",
    "Static application info; value is always 1, metadata carried in labels.",
    labelnames=("service", "version", "env"),
    registry=registry,
)

# --------------------------------------------------------------------------- #
# Per-shot render telemetry (§12.5)
# --------------------------------------------------------------------------- #

#: Histogram bucket edges for end-to-end render latency (seconds). Wan renders
#: can take minutes, so the upper buckets are generous.
_LATENCY_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)
#: Histogram bucket edges for QA metrics (all live in ``[0, 1]``; the extra
#: edges straddle the §9.5 thresholds CCS≥0.85 / style≤0.08 / motion≤0.25).
_QA_BUCKETS: tuple[float, ...] = (
    0.05, 0.08, 0.1, 0.15, 0.2, 0.25, 0.5, 0.75, 0.85, 0.9, 0.95, 1.0,
)

render_latency_seconds: Histogram = Histogram(
    "kinora_render_latency_seconds",
    "Wall-clock latency of a per-shot render, labelled by the mode/rung served.",
    labelnames=("mode",),
    buckets=_LATENCY_BUCKETS,
    registry=registry,
)

qa_score: Histogram = Histogram(
    "kinora_qa_score",
    "Critic QA scores (label metric=ccs|style_drift|motion) per accepted shot.",
    labelnames=("metric",),
    buckets=_QA_BUCKETS,
    registry=registry,
)

render_retries_total: Counter = Counter(
    "kinora_render_retries_total",
    "Total §9.5 repair retries beyond the first attempt across all shots.",
    registry=registry,
)

cache_hits_total: Counter = Counter(
    "kinora_cache_hits_total",
    "Shot-cache hits (a clip served at zero video-seconds, §8.7).",
    registry=registry,
)

cache_misses_total: Counter = Counter(
    "kinora_cache_misses_total",
    "Shot-cache misses (a shot had to be designed + rendered/degraded).",
    registry=registry,
)

video_seconds_spent_total: Counter = Counter(
    "kinora_video_seconds_spent_total",
    "Total Wan video-seconds spent (the budget-critical resource, §11.1).",
    registry=registry,
)

render_mode_total: Counter = Counter(
    "kinora_render_mode_total",
    "Shots rendered by Wan render mode used (the §9.3 decision-tree distribution).",
    labelnames=("mode",),
    registry=registry,
)

shots_accepted_total: Counter = Counter(
    "kinora_shots_accepted_total",
    "Shots that passed QA and were accepted as full footage.",
    registry=registry,
)

shots_degraded_total: Counter = Counter(
    "kinora_shots_degraded_total",
    "Shots that fell to the degradation ladder (Ken-Burns / audio card, §12.4).",
    registry=registry,
)

conflicts_total: Counter = Counter(
    "kinora_conflicts_total",
    "Continuity conflicts surfaced to the director for a choice (§7.2).",
    registry=registry,
)

conflicts_resolved_total: Counter = Counter(
    "kinora_conflicts_resolved_total",
    "Continuity conflicts resolved, labelled by the chosen §7.2 policy option "
    "(honor_canon / evolve_canon / surface_to_user).",
    labelnames=("option",),
    registry=registry,
)

# --------------------------------------------------------------------------- #
# Per-session scheduler telemetry (§12.5)
# --------------------------------------------------------------------------- #

buffer_occupancy_seconds: Gauge = Gauge(
    "kinora_buffer_occupancy_seconds",
    "Committed video-seconds buffered ahead of the focus playhead (the §4.10 "
    "sawtooth), labelled by session. The session set is bounded (LRU).",
    labelnames=("session",),
    registry=registry,
)

watermark_crossings_total: Counter = Counter(
    "kinora_watermark_crossings_total",
    "Dual-watermark hysteresis crossings (direction=low starts a burst, "
    "direction=high stops it, §4.5).",
    labelnames=("direction",),
    registry=registry,
)

promotions_total: Counter = Counter(
    "kinora_promotions_total",
    "Shots promoted to the committed render lane (§4.6).",
    registry=registry,
)

idle_periods_total: Counter = Counter(
    "kinora_idle_periods_total",
    "Idle-pause periods entered (speculation halted after the reader went quiet, §4.7).",
    registry=registry,
)

seek_events_total: Counter = Counter(
    "kinora_seek_events_total",
    "Reader seeks handled (cancel-distant + bridge + re-seed, §4.8).",
    registry=registry,
)

# --------------------------------------------------------------------------- #
# Render-queue telemetry (§12.1–§12.3)
# --------------------------------------------------------------------------- #

queue_depth: Gauge = Gauge(
    "kinora_queue_depth",
    "Current queued depth per render lane (committed/speculative/keyframe).",
    labelnames=("lane",),
    registry=registry,
)

jobs_total: Counter = Counter(
    "kinora_jobs_total",
    "Render jobs by lifecycle status (enqueued/dropped/succeeded/retrying/"
    "deadletter/cancelled).",
    labelnames=("status",),
    registry=registry,
)

dlq_total: Counter = Counter(
    "kinora_dlq_total",
    "Jobs dead-lettered after exhausting retries (§12.1).",
    registry=registry,
)

cancellations_total: Counter = Counter(
    "kinora_cancellations_total",
    "Jobs finalized as cancelled at a safe point (budget released, §4.8/§12.1).",
    registry=registry,
)

# --------------------------------------------------------------------------- #
# Provider telemetry (DashScope calls; §11/§12.5)
# --------------------------------------------------------------------------- #

provider_calls_total: Counter = Counter(
    "kinora_provider_calls_total",
    "DashScope provider calls by model + operation (terminal outcomes only).",
    labelnames=("model", "op"),
    registry=registry,
)

provider_latency_seconds: Histogram = Histogram(
    "kinora_provider_latency_seconds",
    "Latency of successful provider calls by operation.",
    labelnames=("op",),
    buckets=_LATENCY_BUCKETS,
    registry=registry,
)

provider_tokens_total: Counter = Counter(
    "kinora_provider_tokens_total",
    "Tokens billed by model + direction (input/output).",
    labelnames=("model", "direction"),
    registry=registry,
)

provider_errors_total: Counter = Counter(
    "kinora_provider_errors_total",
    "Provider calls that ultimately failed by model + operation.",
    labelnames=("model", "op"),
    registry=registry,
)


# --------------------------------------------------------------------------- #
# App-info / HTTP helpers (Phase 9)
# --------------------------------------------------------------------------- #


def set_app_info(*, service: str, version: str, env: str) -> None:
    """Set the single ``app_info`` series describing this process."""
    app_info.labels(service=service, version=version, env=env).set(1)


def record_request(method: str, path: str, status: int) -> None:
    """Increment the request counter for a completed HTTP request."""
    http_requests_total.labels(method=method, path=path, status=str(status)).inc()


# --------------------------------------------------------------------------- #
# Per-shot emit helpers
# --------------------------------------------------------------------------- #


def observe_render_latency(mode: str, seconds: float) -> None:
    """Record one per-shot render latency, labelled by the mode/rung served."""
    render_latency_seconds.labels(mode=mode).observe(max(seconds, 0.0))


def observe_qa(*, ccs: float | None, style_drift: float | None, motion: float | None) -> None:
    """Record the Critic's QA scores for an accepted shot (skips ``None``)."""
    if ccs is not None:
        qa_score.labels(metric="ccs").observe(ccs)
    if style_drift is not None:
        qa_score.labels(metric="style_drift").observe(style_drift)
    if motion is not None:
        qa_score.labels(metric="motion").observe(motion)


def inc_render_retries(count: int) -> None:
    """Add ``count`` repair retries (attempts beyond the first) to the counter."""
    if count > 0:
        render_retries_total.inc(count)


def inc_cache(*, hit: bool) -> None:
    """Increment the shot-cache hit or miss counter."""
    (cache_hits_total if hit else cache_misses_total).inc()


def inc_video_seconds(seconds: float) -> None:
    """Add spent Wan video-seconds to the running total."""
    if seconds > 0:
        video_seconds_spent_total.inc(seconds)


def inc_render_mode(mode: str) -> None:
    """Count one shot rendered with the given Wan render mode."""
    render_mode_total.labels(mode=mode).inc()


def inc_shot_accepted() -> None:
    """Count one accepted (QA-passed) shot."""
    shots_accepted_total.inc()


def inc_shot_degraded() -> None:
    """Count one shot that fell to the degradation ladder."""
    shots_degraded_total.inc()


def inc_conflict() -> None:
    """Count one continuity conflict surfaced to the director."""
    conflicts_total.inc()


def inc_conflict_resolved(option: str) -> None:
    """Count one §7.2 conflict resolution, by the chosen policy option."""
    conflicts_resolved_total.labels(option=option).inc()


# --------------------------------------------------------------------------- #
# Per-session emit helpers (bounded session-label cardinality)
# --------------------------------------------------------------------------- #

#: Hard cap on the number of distinct sessions carrying a buffer-occupancy
#: series, so a churn of session ids can never blow up cardinality.
MAX_SESSION_SERIES = 512
_session_lru: OrderedDict[str, None] = OrderedDict()
_session_lock = threading.Lock()


def set_buffer_occupancy(session: str, seconds: float) -> None:
    """Set a session's committed-buffer occupancy gauge (bounded LRU set).

    When the tracked-session set is full the least-recently-updated session's
    series is evicted, so the gauge's cardinality stays bounded regardless of
    how many sessions come and go.
    """
    with _session_lock:
        if session in _session_lru:
            _session_lru.move_to_end(session)
        else:
            if len(_session_lru) >= MAX_SESSION_SERIES:
                oldest, _ = _session_lru.popitem(last=False)
                _safe_remove_session(oldest)
            _session_lru[session] = None
    buffer_occupancy_seconds.labels(session=session).set(seconds)


def clear_session_metrics(session: str) -> None:
    """Drop a session's buffer-occupancy series (call on session end)."""
    with _session_lock:
        _session_lru.pop(session, None)
    _safe_remove_session(session)


def _safe_remove_session(session: str) -> None:
    with contextlib.suppress(KeyError):
        buffer_occupancy_seconds.remove(session)


def inc_watermark_crossing(direction: str) -> None:
    """Count a dual-watermark crossing (``direction`` is ``low`` or ``high``)."""
    watermark_crossings_total.labels(direction=direction).inc()


def inc_promotions(count: int) -> None:
    """Add ``count`` committed-lane promotions to the counter."""
    if count > 0:
        promotions_total.inc(count)


def inc_idle_period() -> None:
    """Count one idle-pause period entered."""
    idle_periods_total.inc()


def inc_seek_event() -> None:
    """Count one reader seek handled."""
    seek_events_total.inc()


# --------------------------------------------------------------------------- #
# Queue emit helpers
# --------------------------------------------------------------------------- #


def set_queue_depth(lane: str, depth: int) -> None:
    """Set the current queued depth for a render lane."""
    queue_depth.labels(lane=lane).set(depth)


def inc_job(status: str, count: int = 1) -> None:
    """Count ``count`` jobs transitioning into ``status``."""
    if count > 0:
        jobs_total.labels(status=status).inc(count)


def inc_dlq() -> None:
    """Count one dead-lettered job."""
    dlq_total.inc()


def inc_cancellations(count: int = 1) -> None:
    """Count ``count`` cancelled jobs."""
    if count > 0:
        cancellations_total.inc(count)


# --------------------------------------------------------------------------- #
# Provider emit helpers
# --------------------------------------------------------------------------- #


def observe_provider(
    *, model: str, op: str, latency_s: float | None = None, ok: bool = True
) -> None:
    """Record a terminal provider call: always a call, latency if ok, else an error."""
    provider_calls_total.labels(model=model, op=op).inc()
    if ok:
        if latency_s is not None:
            provider_latency_seconds.labels(op=op).observe(max(latency_s, 0.0))
    else:
        provider_errors_total.labels(model=model, op=op).inc()


def inc_provider_tokens(*, model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
    """Add input/output tokens billed for a provider call."""
    if input_tokens:
        provider_tokens_total.labels(model=model, direction="input").inc(input_tokens)
    if output_tokens:
        provider_tokens_total.labels(model=model, direction="output").inc(output_tokens)


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #


def render_metrics() -> tuple[bytes, str]:
    """Return the exposition payload and its content type for ``/metrics``."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
