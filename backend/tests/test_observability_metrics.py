"""Observability: the Prometheus §12.5 series are defined, exposed, and emitted.

Three things are checked:

* every §12.5 per-shot / per-session / queue / provider series name appears in
  the ``/metrics`` exposition (scraped via the meta-endpoint test client);
* the typed emit helpers move the right counters/gauges;
* a *simulated* render path (the same injected doubles the pipeline tests use)
  increments the cache, render-mode, and video-seconds counters — no DB, no
  DashScope, no real Wan render.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.observability import metrics
from app.observability.metrics import registry
from app.render import degrade

#: Every series the §12.5 telemetry surface must expose.
_EXPECTED_SERIES = [
    # per-shot
    "kinora_render_latency_seconds",
    "kinora_qa_score",
    "kinora_render_retries_total",
    "kinora_cache_hits_total",
    "kinora_cache_misses_total",
    "kinora_video_seconds_spent_total",
    "kinora_render_mode_total",
    "kinora_shots_accepted_total",
    "kinora_shots_degraded_total",
    "kinora_conflicts_total",
    # per-session
    "kinora_buffer_occupancy_seconds",
    "kinora_watermark_crossings_total",
    "kinora_promotions_total",
    "kinora_idle_periods_total",
    "kinora_seek_events_total",
    # queue
    "kinora_queue_depth",
    "kinora_jobs_total",
    "kinora_dlq_total",
    "kinora_cancellations_total",
    # provider
    "kinora_provider_calls_total",
    "kinora_provider_latency_seconds",
    "kinora_provider_tokens_total",
    "kinora_provider_errors_total",
]


def _val(name: str, labels: dict[str, str] | None = None) -> float:
    """Current sample value (0.0 when the series/label-set has no sample yet)."""
    value = registry.get_sample_value(name, labels or {})
    return value if value is not None else 0.0


async def test_metrics_endpoint_exposes_all_phase125_series(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    missing = [name for name in _EXPECTED_SERIES if name not in body]
    assert not missing, f"missing §12.5 series in /metrics: {missing}"


async def test_emit_helpers_move_counters() -> None:
    before_hits = _val("kinora_cache_hits_total")
    before_miss = _val("kinora_cache_misses_total")
    before_video = _val("kinora_video_seconds_spent_total")
    before_promo = _val("kinora_promotions_total")
    before_low = _val("kinora_watermark_crossings_total", {"direction": "low"})

    metrics.inc_cache(hit=True)
    metrics.inc_cache(hit=False)
    metrics.inc_video_seconds(4.5)
    metrics.inc_promotions(2)
    metrics.inc_watermark_crossing("low")

    assert _val("kinora_cache_hits_total") == before_hits + 1
    assert _val("kinora_cache_misses_total") == before_miss + 1
    assert _val("kinora_video_seconds_spent_total") == pytest.approx(before_video + 4.5)
    assert _val("kinora_promotions_total") == before_promo + 2
    assert _val("kinora_watermark_crossings_total", {"direction": "low"}) == before_low + 1


async def test_provider_helpers_record_calls_latency_tokens_errors() -> None:
    calls = _val("kinora_provider_calls_total", {"model": "qwen-test", "op": "chat"})
    errors = _val("kinora_provider_errors_total", {"model": "qwen-test", "op": "chat"})
    tok_in = _val("kinora_provider_tokens_total", {"model": "qwen-test", "direction": "input"})

    metrics.observe_provider(model="qwen-test", op="chat", latency_s=0.2, ok=True)
    metrics.observe_provider(model="qwen-test", op="chat", ok=False)
    metrics.inc_provider_tokens(model="qwen-test", input_tokens=30, output_tokens=7)

    assert _val("kinora_provider_calls_total", {"model": "qwen-test", "op": "chat"}) == calls + 2
    assert _val("kinora_provider_errors_total", {"model": "qwen-test", "op": "chat"}) == errors + 1
    assert _val(
        "kinora_provider_tokens_total", {"model": "qwen-test", "direction": "input"}
    ) == tok_in + 30


async def test_buffer_occupancy_gauge_is_bounded_and_clearable() -> None:
    metrics.set_buffer_occupancy("sess_obs_1", 42.0)
    assert _val("kinora_buffer_occupancy_seconds", {"session": "sess_obs_1"}) == 42.0
    # The session-labelled gauge can be dropped on session end (no leak).
    metrics.clear_session_metrics("sess_obs_1")
    assert registry.get_sample_value(
        "kinora_buffer_occupancy_seconds", {"session": "sess_obs_1"}
    ) is None


@pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")
async def test_simulated_render_path_increments_cache_mode_and_video_seconds() -> None:
    # Reuse the exact injected-double bundle the pipeline tests drive.
    from tests.test_render_pipeline import _PASS, make_bundle
    from tests.test_render_support import BOOK_ID, SHOT_ID

    before_miss = _val("kinora_cache_misses_total")
    before_mode = _val("kinora_render_mode_total", {"mode": "reference_to_video"})
    before_video = _val("kinora_video_seconds_spent_total")
    before_accepted = _val("kinora_shots_accepted_total")

    bundle = make_bundle(critic_metrics=[_PASS], budget_live=True)
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)
    assert result.rung == "full_video"

    assert _val("kinora_cache_misses_total") == before_miss + 1
    assert _val("kinora_render_mode_total", {"mode": "reference_to_video"}) == before_mode + 1
    assert _val("kinora_video_seconds_spent_total") == pytest.approx(before_video + 5.0)
    assert _val("kinora_shots_accepted_total") == before_accepted + 1
    # Latency was observed for the served mode (the histogram has a count sample).
    assert _val("kinora_render_latency_seconds_count", {"mode": "full_video"}) >= 1
