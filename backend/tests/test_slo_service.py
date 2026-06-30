"""Tests for the process-wide SLO service helpers + the real-probe builder."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.slo.health import Criticality, HealthStatus
from app.slo.service import (
    build_health_registry,
    get_health_registry,
    get_slo_engine,
    observe_intent_latency_ms,
    observe_render_latency_ms,
    record_api_request,
    record_read,
    record_shot,
    reset_for_test,
    set_slo_engine,
)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    reset_for_test()
    yield
    reset_for_test()


def test_emit_helpers_feed_the_singleton_engine() -> None:
    now = 500.0
    for _ in range(9):
        record_read(underrun_free=True, now=now)
    record_read(underrun_free=False, now=now)
    record_shot(accepted=True, now=now)
    record_api_request(ok=True, now=now)
    observe_render_latency_ms(3000.0, now=now)
    observe_intent_latency_ms(120.0, now=now)

    status = get_slo_engine().status(now=now)
    read = next(b for b in status.budgets if b.objective.name == "read-underrun-free")
    assert read.good_ratio == pytest.approx(0.9)


def test_set_slo_engine_overrides_singleton() -> None:
    from app.slo.engine import build_default_engine

    engine = build_default_engine(read_target=0.5)
    set_slo_engine(engine)
    assert get_slo_engine() is engine


def test_get_health_registry_is_stable_singleton() -> None:
    assert get_health_registry() is get_health_registry()


async def test_build_health_registry_probes_real_deps() -> None:
    class _FakeRedis:
        async def ping(self) -> bool:
            return True

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def execute(self, *_a: object, **_k: object) -> None:
            return None

    class _FakeStore:
        async def health_check(self) -> None:
            return None

    class _FakeContainer:
        redis = _FakeRedis()
        object_store = _FakeStore()

        def sessionmaker(self) -> _FakeSession:
            return _FakeSession()

    reg = build_health_registry(_FakeContainer())  # type: ignore[arg-type]
    names = {p.name: p.criticality for p in reg.probes}
    assert names["postgres"] is Criticality.CRITICAL
    assert names["redis"] is Criticality.CRITICAL
    assert names["object_store"] is Criticality.OPTIONAL

    report = await reg.readiness()
    assert report.ready is True
    assert report.status is HealthStatus.UP


async def test_build_health_registry_object_store_without_hook_degrades() -> None:
    class _FakeRedis:
        async def ping(self) -> bool:
            return True

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def execute(self, *_a: object, **_k: object) -> None:
            return None

    class _FakeContainer:
        redis = _FakeRedis()
        object_store = object()  # no health hook, no exists()

        def sessionmaker(self) -> _FakeSession:
            return _FakeSession()

    reg = build_health_registry(_FakeContainer())  # type: ignore[arg-type]
    report = await reg.readiness()
    store = next(o for o in report.outcomes if o.name == "object_store")
    assert store.status is HealthStatus.DEGRADED
    assert report.ready is True  # optional => still ready
