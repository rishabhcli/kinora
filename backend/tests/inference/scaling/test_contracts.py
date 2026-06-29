"""Unit tests for the consumed protocols + fakes (app...scaling.contracts)."""

from __future__ import annotations

import pytest

from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendHealth,
    BackendKind,
    BackendTelemetry,
    FakeBackend,
    FakeMetrics,
    InferenceBackend,
    RouterMetricsSource,
)
from app.reliability.latency import LatencyDigest


def _summary(*ms: float):  # type: ignore[no-untyped-def]
    d = LatencyDigest()
    for m in ms or (1000.0,):
        d.record_ms(m)
    return d.summary()


def _descriptor(**kw: object) -> BackendDescriptor:
    base: dict[str, object] = {
        "backend_id": "wan@a10",
        "kind": BackendKind.VIDEO,
        "instance_type": "gpu-a10",
    }
    base.update(kw)
    return BackendDescriptor(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Descriptor
# --------------------------------------------------------------------------- #


def test_descriptor_throughput() -> None:
    d = _descriptor(concurrency=2, service_time_s=4.0)
    assert d.throughput_per_worker_per_s == pytest.approx(0.5)


def test_descriptor_validation() -> None:
    with pytest.raises(ValueError):
        _descriptor(concurrency=0)
    with pytest.raises(ValueError):
        _descriptor(service_time_s=0.0)


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #


def test_telemetry_utilisation() -> None:
    t = BackendTelemetry(
        backend_id="b", warm_workers=4, inflight=2, queue_depth=0, latency=_summary()
    )
    assert t.utilisation == pytest.approx(0.5)


def test_telemetry_routable_flags() -> None:
    healthy = BackendTelemetry("b", 1, 0, 0, _summary(), BackendHealth.HEALTHY)
    degraded = BackendTelemetry("b", 1, 0, 0, _summary(), BackendHealth.DEGRADED)
    unhealthy = BackendTelemetry("b", 1, 0, 0, _summary(), BackendHealth.UNHEALTHY)
    assert healthy.is_routable
    assert degraded.is_routable  # degraded still serves
    assert not unhealthy.is_routable


def test_telemetry_handles_zero_warm() -> None:
    t = BackendTelemetry("b", warm_workers=0, inflight=0, queue_depth=5, latency=_summary())
    # Avoids div-by-zero (treats as 1 slot).
    assert t.utilisation == 0.0


# --------------------------------------------------------------------------- #
# Fakes satisfy the runtime-checkable protocols (structural conformance)
# --------------------------------------------------------------------------- #


def test_fake_backend_satisfies_protocol() -> None:
    fb = FakeBackend(_descriptor())
    assert isinstance(fb, InferenceBackend)
    assert fb.descriptor().backend_id == "wan@a10"


def test_fake_metrics_satisfies_protocol() -> None:
    fm = FakeMetrics()
    assert isinstance(fm, RouterMetricsSource)
    tel = BackendTelemetry("b1", 2, 1, 0, _summary())
    fm.set(tel)
    assert fm.backend_ids() == ("b1",)
    assert fm.telemetry("b1") is tel


def test_fake_metrics_replace_snapshot() -> None:
    fm = FakeMetrics()
    fm.set(BackendTelemetry("b1", 1, 0, 0, _summary()))
    fm.set(BackendTelemetry("b1", 5, 3, 1, _summary()))
    assert fm.telemetry("b1").warm_workers == 5
