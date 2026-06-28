"""Telemetry: RED (API) + USE (workers) helpers and the domain facade.

prometheus_client is a hard dependency of the backend, so the RED/USE histograms
register against the shared registry here and the values are scraped back out of
the exposition. The span side is asserted via an in-memory exporter.
"""

from __future__ import annotations

from app.observability.metrics import render_metrics
from app.telemetry import context as ctx
from app.telemetry import domain, red, spans, use
from app.telemetry.exporters import InMemorySpanExporter
from app.telemetry.warehouse import MetricsWarehouse


def _install_exporter() -> InMemorySpanExporter:
    ctx.clear_context()
    exporter = InMemorySpanExporter()
    spans.set_tracer(spans.Tracer(exporter=exporter))
    return exporter


def _scrape() -> str:
    payload, _ = render_metrics()
    return payload.decode()


# --------------------------------------------------------------------------- #
# RED
# --------------------------------------------------------------------------- #


def test_track_request_emits_duration_and_binds_correlation() -> None:
    exporter = _install_exporter()
    with red.track_request("GET", "/api/sessions/{id}", correlation_id="corr_red") as rc:
        assert ctx.get_correlation_id() == "corr_red"
        rc["status"] = 200
    # Correlation id is cleared after the request scope.
    assert ctx.get_correlation_id() is None
    span = exporter.finished_spans()[0]
    assert span.name == "http.request"
    assert span.attributes["http.status_code"] == 200
    assert span.status == spans.STATUS_OK
    text = _scrape()
    assert "kinora_http_request_duration_seconds" in text


def test_track_request_marks_5xx_as_error_span() -> None:
    exporter = _install_exporter()
    with red.track_request("POST", "/api/x") as rc:
        rc["status"] = 503
    span = exporter.finished_spans()[0]
    assert span.status == spans.STATUS_ERROR


def test_track_request_defaults_to_500_on_unhandled_exception() -> None:
    exporter = _install_exporter()
    try:
        with red.track_request("GET", "/api/boom"):
            raise ValueError("x")
    except ValueError:
        pass
    span = exporter.finished_spans()[0]
    # Status was never set → defaulted to 500 → error span.
    assert span.status == spans.STATUS_ERROR


def test_observe_request_duration_is_safe_to_call_directly() -> None:
    red.observe_request_duration("GET", "/api/y", 200, 0.123)
    assert "kinora_http_request_duration_seconds" in _scrape()


# --------------------------------------------------------------------------- #
# USE
# --------------------------------------------------------------------------- #


def test_track_job_emits_duration_utilization_and_span() -> None:
    exporter = _install_exporter()
    with use.track_job("committed", worker="render-worker") as jc:
        jc["outcome"] = "succeeded"
    span = exporter.finished_spans()[0]
    assert span.name == "job.render"
    assert span.attributes["job.outcome"] == "succeeded"
    text = _scrape()
    assert "kinora_job_duration_seconds" in text
    assert "kinora_worker_busy_ratio" in text


def test_track_job_continues_remote_trace() -> None:
    exporter = _install_exporter()
    # Producer stamps a carrier.
    with spans.span("enqueue"):
        carrier = spans.inject_context()
    producer_trace, _ = spans.parse_traceparent(carrier[spans.TRACEPARENT_HEADER])
    ctx.clear_context()
    # Worker continues it.
    with use.track_job("speculative", carrier=carrier) as jc:
        jc["outcome"] = "succeeded"
    job_span = next(s for s in exporter.finished_spans() if s.name == "job.render")
    assert job_span.trace_id == producer_trace


def test_track_job_failure_outcome_marks_error() -> None:
    exporter = _install_exporter()
    with use.track_job("keyframe") as jc:
        jc["outcome"] = "deadletter"
    span = exporter.finished_spans()[0]
    assert span.status == spans.STATUS_ERROR


def test_set_worker_busy_ratio_clamps() -> None:
    use.set_worker_busy_ratio("w1", 2.5)
    use.set_worker_busy_ratio("w1", -1.0)
    assert "kinora_worker_busy_ratio" in _scrape()


# --------------------------------------------------------------------------- #
# Domain facade
# --------------------------------------------------------------------------- #


def test_domain_record_qa_updates_prometheus_and_warehouse() -> None:
    wh = MetricsWarehouse()
    domain.record_qa(ccs=0.9, style_drift=0.05, motion=0.1, warehouse=wh)
    stats = wh.agent("generator")
    assert stats is not None
    assert abs((stats.mean_ccs or 0) - 0.9) < 1e-9
    assert "kinora_qa_score" in _scrape()


def test_domain_record_shot_outcome_updates_both() -> None:
    wh = MetricsWarehouse()
    domain.record_shot_outcome(accepted=True, video_seconds=5.0, warehouse=wh)
    domain.record_shot_outcome(accepted=False, regenerations=1, warehouse=wh)
    stats = wh.agent("generator")
    assert stats is not None
    assert stats.shots_accepted == 1
    assert stats.shots_degraded == 1
    text = _scrape()
    assert "kinora_shots_accepted_total" in text
    assert "kinora_shots_degraded_total" in text


def test_domain_buffer_and_budget_helpers() -> None:
    domain.record_buffer_occupancy("sess_dom", 42.0)
    domain.record_watermark_crossing("low")
    domain.record_budget_burn(3.0)
    domain.record_conflict()
    domain.record_conflict_resolved("honor_canon")
    text = _scrape()
    assert "kinora_buffer_occupancy_seconds" in text
    assert "kinora_watermark_crossings_total" in text
    assert "kinora_video_seconds_spent_total" in text
    assert "kinora_conflicts_resolved_total" in text


def test_publish_warehouse_to_prometheus_mirrors_gauges() -> None:
    wh = MetricsWarehouse()
    wh.record_agent_call("generator", input_tokens=10, output_tokens=20, cost_usd=0.5)
    wh.record_qa("generator", ccs=0.88)
    domain.publish_warehouse_to_prometheus(wh)
    text = _scrape()
    assert "kinora_agent_calls_total_gauge" in text
    assert "kinora_agent_cost_usd_gauge" in text
    assert 'agent="generator"' in text
