"""Observability plane + registry snapshot — the DI seam and read side.

Asserts the plane defaults to cheap no-ops (null exporter, no exposition, empty
timeline), wires the in-memory span ring on demand so a render produces a
reconstructable timeline, gates the Prometheus exposition behind the flag, and
that the typed registry snapshot reads counters/gauges/histograms + derived SLIs.
All offline; the OpenTelemetry SDK is not required for any of it.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.observability import facade
from app.observability.plane import ObservabilityPlane, reset_tracer_to_default
from app.observability.registry import snapshot
from app.telemetry import context as ctx
from app.telemetry.exporters import InMemorySpanExporter, NullSpanExporter


@pytest.fixture(autouse=True)
def _reset() -> None:
    ctx.clear_context()
    reset_tracer_to_default()
    yield
    reset_tracer_to_default()


# --------------------------------------------------------------------------- #
# Plane defaults / no-op fallback.
# --------------------------------------------------------------------------- #


def test_noop_plane_installs_null_exporter_and_empty_timeline() -> None:
    plane = ObservabilityPlane.noop()
    exporter = plane.install_tracing()
    assert isinstance(exporter, NullSpanExporter)
    with facade.span("x", shot_id="s"):
        pass
    # Spans are dropped → the timeline is empty (no retention overhead).
    assert plane.timeline().span_count == 0


def test_noop_plane_has_no_metrics_router() -> None:
    assert ObservabilityPlane.noop().metrics_router() is None


def test_span_path_works_with_otel_absent_via_pure_tracer() -> None:
    # Regardless of the OTel SDK being installed, the dependency-free tracer
    # records the span — this is the no-op-when-otel-absent fallback.
    plane = ObservabilityPlane(collect_spans=True)
    plane.install_tracing()
    with facade.span("render.shot", shot_id="s1"), facade.span("provider.i2v"):
        pass
    tl = plane.timeline()
    assert tl.span_count == 2
    # No OTLP endpoint configured → the bridge stays off; the tree is still built.
    assert not _otlp_endpoint_active()


def _otlp_endpoint_active() -> bool:
    import os

    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


# --------------------------------------------------------------------------- #
# Span collection + timeline reconstruction through the plane.
# --------------------------------------------------------------------------- #


def test_collect_spans_installs_in_memory_ring_and_reconstructs_timeline() -> None:
    plane = ObservabilityPlane(collect_spans=True)
    exporter = plane.install_tracing()
    assert isinstance(exporter, InMemorySpanExporter)

    with (
        facade.render_span("render.shot", shot_id="shot-7", mode="i2v"),
        facade.provider_span("i2v", model="m", shot_id="shot-7"),
    ):
        pass

    tl = plane.timeline()
    assert tl.shot_id == "shot-7"
    assert tl.span_count == 2
    assert tl.roots[0].name == "render.shot"

    by_shot = plane.timelines_by_shot()
    assert "shot-7" in by_shot


def test_clear_spans_drops_collected_spans() -> None:
    plane = ObservabilityPlane(collect_spans=True)
    plane.install_tracing()
    with facade.span("x", shot_id="s"):
        pass
    assert plane.timeline().span_count == 1
    plane.clear_spans()
    assert plane.timeline().span_count == 0


# --------------------------------------------------------------------------- #
# Flag-gated exposition.
# --------------------------------------------------------------------------- #


async def test_metrics_router_serves_exposition_when_enabled() -> None:
    from fastapi import FastAPI

    plane = ObservabilityPlane(metrics_enabled=True)
    router = plane.metrics_router()
    assert router is not None
    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "kinora_render_latency_seconds" in resp.text


def test_metrics_router_is_none_when_flag_off() -> None:
    assert ObservabilityPlane(metrics_enabled=False).metrics_router() is None


# --------------------------------------------------------------------------- #
# from_settings wiring.
# --------------------------------------------------------------------------- #


def test_from_settings_reads_additive_flags() -> None:
    from app.core.config import Settings

    settings = Settings(observability_metrics_enabled=False, observability_collect_spans=True)
    plane = ObservabilityPlane.from_settings(settings)
    assert plane.metrics_enabled is False
    assert plane.collect_spans is True


# --------------------------------------------------------------------------- #
# Registry snapshot (read side) — isolated against a private registry.
# --------------------------------------------------------------------------- #


def test_snapshot_reads_counters_gauges_and_histograms() -> None:
    reg = CollectorRegistry()
    c = Counter("snap_calls_total", "d", labelnames=("model", "op"), registry=reg)
    g = Gauge("snap_depth", "d", labelnames=("lane",), registry=reg)
    h = Histogram("snap_latency_seconds", "d", labelnames=("op",), registry=reg)
    c.labels(model="m", op="i2v").inc(3)
    g.labels(lane="committed").set(7)
    h.labels(op="i2v").observe(0.5)
    h.labels(op="i2v").observe(1.5)

    snap = snapshot(reg)
    assert snap.counter("snap_calls_total", model="m", op="i2v") == 3.0
    assert snap.gauge("snap_depth", lane="committed") == 7.0
    hist = snap.histogram("snap_latency_seconds", op="i2v")
    assert hist.count == 2.0
    assert hist.sum == 2.0
    assert hist.mean == 1.0


def test_snapshot_absent_series_default_to_zero() -> None:
    snap = snapshot(CollectorRegistry())
    assert snap.counter("missing_total") == 0.0
    assert snap.gauge("missing") == 0.0
    assert snap.histogram("missing_seconds").count == 0.0
    assert snap.histogram("missing_seconds").mean == 0.0


def test_snapshot_derived_provider_error_rate() -> None:
    reg = CollectorRegistry()
    calls = Counter("kinora_provider_calls_total", "d", labelnames=("model", "op"), registry=reg)
    errs = Counter("kinora_provider_errors_total", "d", labelnames=("model", "op"), registry=reg)
    calls.labels(model="m", op="i2v").inc(4)
    errs.labels(model="m", op="i2v").inc(1)
    snap = snapshot(reg)
    assert snap.provider_error_rate(model="m", op="i2v") == 0.25
    # No calls → a safe zero, not a divide-by-zero.
    assert snap.provider_error_rate(model="m", op="t2v") == 0.0


def test_snapshot_derived_cache_hit_ratio() -> None:
    reg = CollectorRegistry()
    hits = Counter("kinora_cache_hits_total", "d", registry=reg)
    misses = Counter("kinora_cache_misses_total", "d", registry=reg)
    hits.inc(3)
    misses.inc(1)
    assert snapshot(reg).cache_hit_ratio() == 0.75
    assert snapshot(CollectorRegistry()).cache_hit_ratio() == 0.0
