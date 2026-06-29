"""Tests for app.inference.router.admission — backpressure + queue-time SLA.

Pins the §12.2 backpressure rules: hard ceiling for everyone, a soft zone that
sheds low-priority work while still admitting committed/interactive, per-tenant
concurrency + queue caps, and an immediate reject for an unservable request. The
SLA component is tested against an injected clock.
"""

from __future__ import annotations

import pytest

from app.inference.router.admission import (
    AdmissionConfig,
    AdmissionController,
    LoadSnapshot,
    QueueTimeSLA,
    RejectReason,
)
from app.inference.router.errors import RouterConfigError
from app.inference.router.request import InferenceRequest, RequestPriority


def _req(
    *,
    tenant: str = "t",
    prio: RequestPriority = RequestPriority.COMMITTED,
    tokens: int = 100,
    sla: float | None = None,
    enqueued_at: float = 0.0,
) -> InferenceRequest:
    return InferenceRequest(
        request_id="r",
        model="m",
        tenant=tenant,
        priority=prio,
        prompt_tokens=tokens,
        max_output_tokens=0,
        queue_sla_s=sla,
        enqueued_at=enqueued_at,
    )


def _load(
    *,
    depth: int = 0,
    tenant_inflight: int = 0,
    tenant_queue: int = 0,
    cap: int = 10_000,
) -> LoadSnapshot:
    return LoadSnapshot(
        queue_depth=depth,
        tenant_inflight=tenant_inflight,
        tenant_queue_depth=tenant_queue,
        max_worker_token_capacity=cap,
    )


def test_admits_under_capacity() -> None:
    ctrl = AdmissionController()
    assert ctrl.evaluate(_req(), _load()).admit


def test_hard_ceiling_rejects_everyone() -> None:
    ctrl = AdmissionController(AdmissionConfig(max_queue_depth=10))
    d = ctrl.evaluate(_req(prio=RequestPriority.INTERACTIVE), _load(depth=10))
    assert not d.admit
    assert d.reason is RejectReason.QUEUE_FULL
    assert d.retry_after_s is not None


def test_soft_zone_sheds_low_priority_but_admits_committed() -> None:
    cfg = AdmissionConfig(max_queue_depth=100, soft_queue_depth=50)
    ctrl = AdmissionController(cfg)
    # In the soft zone, speculative is shed...
    spec = ctrl.evaluate(_req(prio=RequestPriority.SPECULATIVE), _load(depth=60))
    assert not spec.admit and spec.reason is RejectReason.SHED_LOW_PRIORITY
    # ...but committed sails through.
    assert ctrl.evaluate(_req(prio=RequestPriority.COMMITTED), _load(depth=60)).admit
    assert ctrl.evaluate(_req(prio=RequestPriority.INTERACTIVE), _load(depth=60)).admit


def test_bulk_shed_in_soft_zone() -> None:
    cfg = AdmissionConfig(max_queue_depth=100, soft_queue_depth=50)
    ctrl = AdmissionController(cfg)
    d = ctrl.evaluate(_req(prio=RequestPriority.BULK), _load(depth=55))
    assert not d.admit and d.reason is RejectReason.SHED_LOW_PRIORITY


def test_below_soft_zone_admits_low_priority() -> None:
    cfg = AdmissionConfig(max_queue_depth=100, soft_queue_depth=50)
    ctrl = AdmissionController(cfg)
    assert ctrl.evaluate(_req(prio=RequestPriority.SPECULATIVE), _load(depth=10)).admit


def test_tenant_concurrency_cap() -> None:
    cfg = AdmissionConfig(max_tenant_inflight=4)
    ctrl = AdmissionController(cfg)
    d = ctrl.evaluate(_req(), _load(tenant_inflight=4))
    assert not d.admit and d.reason is RejectReason.TENANT_CONCURRENCY


def test_tenant_queue_depth_cap() -> None:
    cfg = AdmissionConfig(max_tenant_queue_depth=3)
    ctrl = AdmissionController(cfg)
    d = ctrl.evaluate(_req(), _load(tenant_queue=3))
    assert not d.admit and d.reason is RejectReason.TENANT_QUEUE_FULL


def test_unservable_request_rejected_with_no_retry() -> None:
    ctrl = AdmissionController()
    d = ctrl.evaluate(_req(tokens=5000), _load(cap=4096))
    assert not d.admit
    assert d.reason is RejectReason.UNSERVABLE
    assert d.retry_after_s is None  # never retryable


def test_unservable_takes_precedence_over_queue_full() -> None:
    cfg = AdmissionConfig(max_queue_depth=10)
    ctrl = AdmissionController(cfg)
    d = ctrl.evaluate(_req(tokens=5000), _load(depth=10, cap=4096))
    assert d.reason is RejectReason.UNSERVABLE


def test_admission_config_validation() -> None:
    with pytest.raises(RouterConfigError):
        AdmissionConfig(max_queue_depth=0)
    with pytest.raises(RouterConfigError):
        AdmissionConfig(max_queue_depth=10, soft_queue_depth=20)
    with pytest.raises(RouterConfigError):
        AdmissionConfig(max_tenant_inflight=0)
    with pytest.raises(RouterConfigError):
        AdmissionConfig(default_retry_after_s=-1.0)


# -- queue-time SLA ------------------------------------------------------- #


def test_sla_not_expired_before_deadline() -> None:
    clock = [0.0]
    sla = QueueTimeSLA(clock=lambda: clock[0])
    r = _req(sla=5.0, enqueued_at=0.0)
    clock[0] = 4.9
    assert not sla.is_expired(r)


def test_sla_expired_after_deadline() -> None:
    clock = [0.0]
    sla = QueueTimeSLA(clock=lambda: clock[0])
    r = _req(sla=5.0, enqueued_at=0.0)
    clock[0] = 5.1
    assert sla.is_expired(r)
    assert sla.waited(r) == pytest.approx(5.1)


def test_no_sla_never_expires() -> None:
    sla = QueueTimeSLA()
    r = _req(sla=None, enqueued_at=0.0)
    assert not sla.is_expired(r, now=1e9)


def test_default_sla_backstops_unset_requests() -> None:
    clock = [0.0]
    sla = QueueTimeSLA(default_sla_s=2.0, clock=lambda: clock[0])
    r = _req(sla=None, enqueued_at=0.0)
    clock[0] = 3.0
    assert sla.is_expired(r)


def test_request_sla_overrides_default() -> None:
    clock = [0.0]
    sla = QueueTimeSLA(default_sla_s=100.0, clock=lambda: clock[0])
    r = _req(sla=1.0, enqueued_at=0.0)
    clock[0] = 2.0
    assert sla.is_expired(r)  # request's own 1.0s SLA wins over the 100s default


def test_expired_among_filters() -> None:
    clock = [10.0]
    sla = QueueTimeSLA(clock=lambda: clock[0])
    fresh = InferenceRequest(request_id="fresh", model="m", queue_sla_s=5.0, enqueued_at=8.0)
    stale = InferenceRequest(request_id="stale", model="m", queue_sla_s=1.0, enqueued_at=2.0)
    expired = sla.expired_among([fresh, stale])
    assert [r.request_id for r in expired] == ["stale"]
