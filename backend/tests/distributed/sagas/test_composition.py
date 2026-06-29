"""The composition seam: a wired engine drives the concrete flows end to end."""

from __future__ import annotations

from app.distributed.sagas.composition import build_saga_engine, default_registry
from app.distributed.sagas.flows.fakes import FakeIngestServices, FakeRenderServices
from app.distributed.sagas.flows.ingest import initial_ingest_state
from app.distributed.sagas.flows.render import initial_render_state
from app.distributed.sagas.locks import InMemoryLockManager
from app.distributed.sagas.types import SagaStatus
from app.jobs.clock import ManualClock


def test_default_registry_has_both_flows() -> None:
    reg = default_registry()
    assert reg.has("ingest_canon_identity")
    assert reg.has("render_qa_conflict_degrade")


async def test_engine_drives_ingest_via_worker() -> None:
    ingest = FakeIngestServices()
    engine = build_saga_engine(
        resources={"ingest_ports": ingest},
        clock=ManualClock(),
    )
    await engine.orchestrator.start(
        "ingest_canon_identity",
        "book-1",
        initial_state=initial_ingest_state("book-1", "oss://up/1"),
    )
    driven = await engine.worker.run_until_idle()
    assert driven == 1
    inst = (await engine.store.list_instances(definition="ingest_canon_identity"))[0]
    assert inst.status is SagaStatus.COMPLETED
    assert "book-1" in ingest.ready


async def test_engine_drives_render_via_worker() -> None:
    render = FakeRenderServices()
    engine = build_saga_engine(
        resources={"render_ports": render},
        clock=ManualClock(),
    )
    await engine.orchestrator.run_to_completion(
        "render_qa_conflict_degrade",
        "shot-1",
        initial_state=initial_render_state("shot-1", "h1"),
    )
    inst = (await engine.store.list_instances(definition="render_qa_conflict_degrade"))[0]
    assert inst.status is SagaStatus.COMPLETED
    assert len(render.accepted) == 1


def test_in_memory_backends_by_default() -> None:
    engine = build_saga_engine(clock=ManualClock())
    assert isinstance(engine.locks, InMemoryLockManager)


def test_redis_backends_when_handle_present() -> None:
    """Passing a Redis handle selects the Redis effect ledger + lock manager."""
    from app.distributed.sagas.effects import RedisEffectLedger
    from app.distributed.sagas.locks import RedisLockManager

    class _StubRedis:
        async def eval(self, *a: object, **k: object) -> int:
            return 0

        async def get(self, *a: object, **k: object) -> None:
            return None

    engine = build_saga_engine(redis=_StubRedis(), clock=ManualClock())
    assert isinstance(engine.effects, RedisEffectLedger)
    assert isinstance(engine.locks, RedisLockManager)
