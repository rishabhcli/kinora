"""Unit tests for SLO-driven routing (app.inference.scaling.routing)."""

from __future__ import annotations

from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendHealth,
    BackendKind,
    BackendTelemetry,
)
from app.inference.scaling.instances import default_catalog
from app.inference.scaling.routing import (
    RoutingCandidate,
    RoutingPolicy,
    SLORouter,
)
from app.inference.scaling.workload import RequestPriority
from app.reliability.latency import LatencyDigest


def _summary(*samples_ms: float):  # type: ignore[no-untyped-def]
    d = LatencyDigest()
    for s in samples_ms or (1000.0,):
        d.record_ms(s)
    return d.summary()


def _candidate(
    *,
    name: str,
    instance_key: str,
    warm: int,
    inflight: int,
    queue: int,
    health: BackendHealth = BackendHealth.HEALTHY,
    concurrency: int = 1,
    service_s: float = 5.0,
) -> RoutingCandidate:
    cat = default_catalog()
    desc = BackendDescriptor(
        backend_id=name,
        kind=BackendKind.VIDEO,
        instance_type=instance_key,
        concurrency=concurrency,
        service_time_s=service_s,
    )
    tel = BackendTelemetry(
        backend_id=name,
        warm_workers=warm,
        inflight=inflight,
        queue_depth=queue,
        latency=_summary(),
        health=health,
    )
    return RoutingCandidate(descriptor=desc, instance=cat[instance_key], telemetry=tel)


# --------------------------------------------------------------------------- #
# Healthy filtering
# --------------------------------------------------------------------------- #


def test_no_route_when_all_unhealthy() -> None:
    router = SLORouter()
    c = _candidate(
        name="a", instance_key="gpu-a10", warm=2, inflight=0, queue=0,
        health=BackendHealth.UNHEALTHY,
    )
    d = router.route(candidates=[c], priority=RequestPriority.COMMITTED)
    assert not d.routed
    assert d.backend_id is None
    assert "no healthy" in d.reason


def test_skips_unhealthy_picks_healthy() -> None:
    router = SLORouter(RoutingPolicy(target_tail_s=120.0))
    bad = _candidate(
        name="bad", instance_key="gpu-h20", warm=2, inflight=0, queue=0,
        health=BackendHealth.UNHEALTHY,
    )
    good = _candidate(name="good", instance_key="gpu-l20", warm=2, inflight=0, queue=0)
    d = router.route(candidates=[bad, good], priority=RequestPriority.SPECULATIVE)
    assert d.backend_id == "good"


# --------------------------------------------------------------------------- #
# Cost-first vs fast-first
# --------------------------------------------------------------------------- #


def test_speculative_prefers_cheapest_within_budget() -> None:
    # gpu-a10 is the cheapest on-demand *per request* (conc 2, 5s); gpu-h20 is
    # faster but dearer per request. Cost-first picks the a10.
    router = SLORouter(RoutingPolicy(target_tail_s=300.0))
    cheap = _candidate(name="a10", instance_key="gpu-a10", warm=4, inflight=0, queue=0)
    dear = _candidate(name="h20", instance_key="gpu-h20", warm=4, inflight=0, queue=0)
    d = router.route(candidates=[dear, cheap], priority=RequestPriority.SPECULATIVE)
    # Both meet the (loose) budget => pick the cheaper a10.
    assert d.backend_id == "a10"
    assert d.met_slo


def test_committed_prefers_fastest_within_budget() -> None:
    router = SLORouter(RoutingPolicy(target_tail_s=300.0, committed_prefers_fast=True))
    cheap = _candidate(name="a10", instance_key="gpu-a10", warm=4, inflight=0, queue=0)
    dear = _candidate(name="h20", instance_key="gpu-h20", warm=4, inflight=0, queue=0)
    d = router.route(candidates=[cheap, dear], priority=RequestPriority.COMMITTED)
    # Both meet budget, committed biases to the fast H20 (lowest effective service).
    assert d.backend_id == "h20"


def test_committed_fast_bias_can_be_disabled() -> None:
    router = SLORouter(RoutingPolicy(target_tail_s=300.0, committed_prefers_fast=False))
    cheap = _candidate(name="a10", instance_key="gpu-a10", warm=4, inflight=0, queue=0)
    dear = _candidate(name="h20", instance_key="gpu-h20", warm=4, inflight=0, queue=0)
    d = router.route(candidates=[cheap, dear], priority=RequestPriority.COMMITTED)
    assert d.backend_id == "a10"  # cost-first now


# --------------------------------------------------------------------------- #
# SLO-at-risk rescue
# --------------------------------------------------------------------------- #


def test_rescue_to_fastest_when_none_within_budget() -> None:
    # Tight budget below even the fast backend's service time: nobody meets it.
    router = SLORouter(RoutingPolicy(target_tail_s=1.0))
    cheap = _candidate(
        name="l20", instance_key="gpu-l20", warm=1, inflight=1, queue=10, service_s=5.0
    )
    dear = _candidate(
        name="h20", instance_key="gpu-h20", warm=1, inflight=1, queue=10, service_s=5.0
    )
    d = router.route(candidates=[cheap, dear], priority=RequestPriority.COMMITTED)
    assert not d.met_slo
    assert d.backend_id == "h20"  # the fastest by effective service time
    assert "at risk" in d.reason


def test_degraded_backend_is_penalised() -> None:
    router = SLORouter(RoutingPolicy(target_tail_s=300.0, degraded_penalty=3.0))
    healthy = _candidate(
        name="l20", instance_key="gpu-l20", warm=2, inflight=1, queue=2,
        health=BackendHealth.HEALTHY,
    )
    degraded = _candidate(
        name="l20b", instance_key="gpu-l20", warm=2, inflight=1, queue=2,
        health=BackendHealth.DEGRADED,
    )
    d_healthy = healthy.projected_tail_s(quantile=0.95)
    d_degraded = degraded.projected_tail_s(quantile=0.95) * 3.0
    assert d_degraded > d_healthy
    # The router prefers the healthy one when costs are equal.
    d = router.route(candidates=[degraded, healthy], priority=RequestPriority.COMMITTED)
    assert d.backend_id == "l20"


def test_loaded_backend_has_higher_projected_tail() -> None:
    idle = _candidate(name="idle", instance_key="gpu-a10", warm=2, inflight=0, queue=0)
    busy = _candidate(name="busy", instance_key="gpu-a10", warm=2, inflight=4, queue=20)
    assert busy.projected_tail_s(quantile=0.95) >= idle.projected_tail_s(quantile=0.95)


def test_decision_to_dict() -> None:
    router = SLORouter(RoutingPolicy(target_tail_s=300.0))
    c = _candidate(name="a10", instance_key="gpu-a10", warm=2, inflight=0, queue=0)
    d = router.route(candidates=[c], priority=RequestPriority.SPECULATIVE)
    payload = d.to_dict()
    assert payload["backend_id"] == "a10"
    assert payload["met_slo"] is True
