"""DB-backed LLM-ops store tests.

Run against a throwaway Postgres and SKIP cleanly when ``KINORA_TEST_DATABASE_URL``
(the isolated ``kinora_llmops_test`` :5433) is unset, mirroring
``test_db_data_layer``. Repositories only ``flush``; the session fixture rolls
back on teardown so each test is isolated.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.llmops.registry import PromptRegistry, VersionStatus
from app.llmops.store import EvalReportStore, PromptVersionStore, RunTraceStore
from app.llmops.tracing import RunTrace, TraceQuery, new_trace_id

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping LLM-ops DB tests"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


async def test_persist_and_hydrate_registry(session: AsyncSession) -> None:
    reg = PromptRegistry()
    reg.register("adapter", "v1 body")
    reg.register("adapter", "v2 body changed", bump="minor")
    reg.rollback("adapter")  # active is back at 1.0.0

    store = PromptVersionStore(session)
    await store.persist_registry(reg)

    hydrated = await store.hydrate_registry()
    assert hydrated.get_active("adapter").version == "1.0.0"
    versions = {r.version for r in hydrated.versions("adapter")}
    assert versions == {"1.0.0", "1.1.0"}
    # exactly one ACTIVE per key
    actives = [r for r in hydrated.versions("adapter") if r.status is VersionStatus.ACTIVE]
    assert len(actives) == 1


async def test_upsert_is_idempotent(session: AsyncSession) -> None:
    reg = PromptRegistry()
    reg.register("k", "body")
    store = PromptVersionStore(session)
    await store.persist_registry(reg)
    await store.persist_registry(reg)  # again — must not duplicate
    hydrated = await store.hydrate_registry()
    assert len(hydrated.versions("k")) == 1


def _trace(**kw: object) -> RunTrace:
    base = {
        "id": new_trace_id(),
        "prompt_key": "adapter",
        "prompt_version": "1.0.0",
        "model": "qwen3.7-plus",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cost_usd": Decimal("0.0012"),
        "latency_ms": 120.0,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return RunTrace(**base)  # type: ignore[arg-type]


async def test_run_trace_record_query_aggregate(session: AsyncSession) -> None:
    store = RunTraceStore(session)
    await store.record(_trace(book_id="bk1"))
    await store.record(_trace(book_id="bk1", model="qwen-vl-max"))
    await store.record(_trace(book_id="bk2", error="boom"))

    by_book = await store.query(TraceQuery(book_id="bk1"))
    assert len(by_book) == 2
    errs = await store.query(TraceQuery(errors_only=True))
    assert len(errs) == 1 and errs[0].error == "boom"

    agg = await store.aggregate(TraceQuery(book_id="bk1"))
    assert agg.count == 2
    assert agg.total_cost_usd == Decimal("0.0024")


async def test_run_trace_get_roundtrip(session: AsyncSession) -> None:
    store = RunTraceStore(session)
    t = _trace(inputs={"page": 1}, output='{"beats": []}', guardrail_decision="sanitize")
    await store.record(t)
    fetched = await store.get(t.id)
    assert fetched is not None
    assert fetched.inputs == {"page": 1}
    assert fetched.guardrail_decision == "sanitize"
    assert fetched.cost_usd == t.cost_usd


async def test_eval_report_store(session: AsyncSession) -> None:
    store = EvalReportStore(session)
    rid = await store.save(
        kind="eval",
        prompt_key="adapter",
        dataset_name="adapter_golden_v1",
        body={"mean_score": 0.9},
    )
    assert await store.get(rid) == {"mean_score": 0.9}
    latest = await store.latest_for("adapter", kind="eval")
    assert latest == {"mean_score": 0.9}
    assert await store.get("missing") is None
