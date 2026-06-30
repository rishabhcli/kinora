"""TestClient coverage for the /api/slo surface (infra-free; deps overridden)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container, get_current_user
from app.slo.api import router
from app.slo.engine import build_default_engine
from app.slo.health import Criticality, HealthRegistry, ProbeResult
from app.slo.service import (
    reset_for_test,
    set_health_registry,
    set_slo_engine,
)


async def _up() -> ProbeResult:
    return ProbeResult.up("ok")


async def _down() -> ProbeResult:
    return ProbeResult.down("boom")


@pytest.fixture(autouse=True)
def _clean_singletons() -> Iterator[None]:
    reset_for_test()
    yield
    reset_for_test()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[get_container] = lambda: object()  # registry pre-wired
    return TestClient(app)


def _seed_healthy_engine(now: float = 1000.0) -> None:
    engine = build_default_engine()
    engine.record_event("read.underrun_free", good=True, now=now, weight=999)
    engine.record_event("read.underrun_free", good=False, now=now, weight=1)
    engine.record_event("shot.success", good=True, now=now, weight=100)
    engine.record_event("api.availability", good=True, now=now, weight=1000)
    engine.record_sample("render.latency_ms", 4000.0, now=now)
    engine.record_sample("api.intent_latency_ms", 100.0, now=now)
    set_slo_engine(engine)


def test_live_endpoint() -> None:
    reg = HealthRegistry()
    set_health_registry(reg)
    resp = _client().get("/api/slo/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive", "live": True}


def test_health_endpoint_ready_200() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("object_store", _down, criticality=Criticality.OPTIONAL)
    set_health_registry(reg)
    resp = _client().get("/api/slo/health")
    assert resp.status_code == 200  # optional down => still ready
    body = resp.json()
    assert body["ready"] is True
    assert body["status"] == "down"  # worst observed status surfaces
    names = {d["name"] for d in body["dependencies"]}
    assert names == {"db", "object_store"}


def test_health_endpoint_503_when_critical_down() -> None:
    reg = HealthRegistry()
    reg.register("db", _down, criticality=Criticality.CRITICAL)
    set_health_registry(reg)
    resp = _client().get("/api/slo/health")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False


def test_status_endpoint() -> None:
    _seed_healthy_engine()
    set_health_registry(HealthRegistry())
    resp = _client().get("/api/slo/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["gate"]["decision"] == "allow"
    assert len(body["slis"]) == 5
    assert len(body["error_budgets"]) == 3
    assert len(body["burn_alerts"]) == 3


def test_budgets_and_alerts_and_gate_endpoints() -> None:
    _seed_healthy_engine()
    set_health_registry(HealthRegistry())
    c = _client()

    budgets = c.get("/api/slo/budgets").json()
    assert {b["objective"] for b in budgets["error_budgets"]} == {
        "read-underrun-free",
        "shot-success",
        "api-availability",
    }

    alerts = c.get("/api/slo/alerts").json()
    assert alerts["any_firing"] is False

    gate = c.get("/api/slo/gate").json()
    assert gate["decision"] == "allow"
    assert gate["can_release"] is True
    assert gate["can_promote_canary"] is True


def test_report_endpoint_is_plain_text() -> None:
    _seed_healthy_engine()
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    set_health_registry(reg)
    resp = _client().get("/api/slo/report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert "SLO status:" in text
    assert "health:" in text
    assert "db" in text


def test_health_lazy_builds_from_container_when_registry_empty() -> None:
    # When no probes are pre-wired, the endpoint builds real probes from the
    # container. A fake container with a failing redis ping => 503.
    class _FakeRedis:
        async def ping(self) -> bool:
            return False

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def execute(self, *_a: object, **_k: object) -> None:
            return None

    class _FakeContainer:
        redis = _FakeRedis()
        object_store = object()

        def sessionmaker(self) -> _FakeSession:
            return _FakeSession()

    set_health_registry(HealthRegistry())  # empty => lazy build path

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[get_container] = lambda: _FakeContainer()
    resp = TestClient(app).get("/api/slo/health")
    # redis ping False => critical down => 503.
    assert resp.status_code == 503
    body = resp.json()
    statuses = {d["name"]: d["status"] for d in body["dependencies"]}
    assert statuses["postgres"] == "up"
    assert statuses["redis"] == "down"
