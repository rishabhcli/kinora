"""Postgres durable saga store + effect ledger (require the isolated saga test DB).

Re-runs the engine over the real Postgres backends to prove the database-level
guarantees hold under the ORM: idempotent start (partial unique index), exclusive
claim (``FOR UPDATE SKIP LOCKED``), durable crash-resume (a fresh orchestrator
over the same store finishes a partially advanced saga), and exactly-once effects
(the unique-key ledger). Gated on ``KINORA_TEST_DATABASE_URL`` so the unit suite
still runs with no infra; the conftest ``_isolate_state`` fixture ensures the
schema via ``create_all`` (which now includes the three ``saga_*`` tables) and
truncates between tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.composition import make_session_factory
from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.db_store import PostgresEffectLedger, PostgresSagaStore
from app.distributed.sagas.definition import SagaRegistry, saga, step
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.types import (
    SagaContext,
    SagaStatus,
    StepFailed,
    StepResult,
    StepStatus,
)
from app.jobs.clock import ManualClock

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping postgres saga store tests"
)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[object]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield make_session_factory(maker)
    finally:
        await engine.dispose()


async def test_idempotent_start_via_partial_unique_index(factory: object) -> None:
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    clock = ManualClock()
    orch = SagaOrchestrator(store, reg, clock=clock)
    a = await orch.start("flow", "dup")
    b = await orch.start("flow", "dup")
    assert a.id == b.id


async def test_forward_commit_persists(factory: object) -> None:
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok(book_id="b1")

    async def s2(ctx: SagaContext) -> StepResult:
        assert ctx.state["book_id"] == "b1"
        return StepResult.ok(done=True)

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1), step("s2", s2)))
    orch = SagaOrchestrator(store, reg, clock=ManualClock())
    inst = await orch.run_to_completion("flow", "c-commit")
    assert inst.status is SagaStatus.COMPLETED
    reloaded = await store.load(inst.id)
    assert reloaded is not None
    assert reloaded.instance.state == {"book_id": "b1", "done": True}
    assert [s.status for s in reloaded.steps] == [StepStatus.COMPLETED, StepStatus.COMPLETED]


async def test_compensation_persists(factory: object) -> None:
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]
    undone: list[str] = []

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    async def s1_comp(ctx: SagaContext) -> StepResult:
        undone.append("s1")
        return StepResult.ok()

    async def boom(ctx: SagaContext) -> StepResult:
        raise StepFailed("x", retryable=False)

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1, compensation=s1_comp), step("boom", boom)))
    orch = SagaOrchestrator(store, reg, clock=ManualClock())
    inst = await orch.run_to_completion("flow", "c-comp")
    assert inst.status is SagaStatus.COMPENSATED
    assert undone == ["s1"]
    reloaded = await store.load(inst.id)
    assert reloaded is not None
    by_name = {s.name: s.status for s in reloaded.steps}
    assert by_name["s1"] is StepStatus.COMPENSATED
    assert by_name["boom"] is StepStatus.FAILED


async def test_fresh_orchestrator_resumes_from_db(factory: object) -> None:
    """A 'crashed' orchestrator is replaced; a fresh one over the same DB finishes."""
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]
    ran: list[str] = []

    def mk(name: str) -> object:
        async def handler(ctx: SagaContext) -> StepResult:
            ran.append(name)
            return StepResult.ok()

        return handler

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", mk("s1")), step("s2", mk("s2"))))  # type: ignore[arg-type]
    clock = ManualClock()
    orch = SagaOrchestrator(store, reg, clock=clock)
    started = await orch.start("flow", "c-resume")

    # Simulate s1 already completed in a crashed process by mutating the DB.
    loaded = await store.load(started.id)
    assert loaded is not None
    inst = loaded.instance
    inst.status = SagaStatus.RUNNING
    inst.cursor = 1
    await store.save_instance(inst)
    s1_rec = loaded.steps[0]
    s1_rec.status = StepStatus.COMPLETED
    await store.save_step(s1_rec)

    fresh = SagaOrchestrator(store, reg, clock=clock)
    final = await fresh.resume(started.id)
    assert final.status is SagaStatus.COMPLETED
    assert ran == ["s2"]  # s1 was NOT re-run


async def test_exclusive_claim_skip_locked(factory: object) -> None:
    """Two workers claiming concurrently never lease the same instance."""
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=ManualClock())
    await orch.start("flow", "c-claim")

    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = await store.claim_due(now=now, lease_seconds=60)
    assert a is not None
    b = await store.claim_due(now=now, lease_seconds=60)
    assert b is None  # the only instance is already leased


async def test_postgres_effect_ledger_exactly_once(factory: object) -> None:
    ledger = PostgresEffectLedger(factory)  # type: ignore[arg-type]
    calls = {"n": 0}

    async def action() -> str:
        calls["n"] += 1
        return "v"

    a = await ledger.once("k1", action)
    b = await ledger.once("k1", action)
    assert a == b == "v"
    assert calls["n"] == 1
    rec = await ledger.get("k1")
    assert rec is not None and rec.result == "v"


async def test_engine_uses_db_effect_ledger_for_exactly_once(factory: object) -> None:
    """A step retried after a crash does not re-apply its ledger-wrapped effect."""
    store = PostgresSagaStore(factory)  # type: ignore[arg-type]
    ledger = PostgresEffectLedger(factory)  # type: ignore[arg-type]
    charges = {"n": 0}
    crash = {"on": True}

    async def s1(ctx: SagaContext) -> StepResult:
        await ctx.effects.once(ctx.effect_key("charge"), _charge)
        if crash["on"]:
            crash["on"] = False
            raise StepFailed("crash after charge")
        return StepResult.ok()

    async def _charge() -> int:
        charges["n"] += 1
        return charges["n"]

    instant = BackoffPolicy(max_attempts=3, base_delay_s=0.0, jitter=False)
    reg = SagaRegistry()
    reg.register(saga("pay", step("s1", s1, retry=instant)))
    orch = SagaOrchestrator(store, reg, clock=ManualClock(), effects=ledger)
    inst = await orch.run_to_completion("pay", "c-once")
    assert inst.status is SagaStatus.COMPLETED
    assert charges["n"] == 1  # exactly once despite the retry
