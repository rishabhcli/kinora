"""FinOpsService over real Postgres — tenant cap, cost ledger, reconciliation.

Real DB integration (SKIP without ``KINORA_TEST_DATABASE_URL``); mirrors
``test_memory_budget.py``. Exercises the FinOps additions on top of the unchanged
``BudgetService`` reserve/commit/release contract (kinora.md §11.1).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.repositories.book import BookRepo
from app.db.repositories.budget import BudgetRepo
from app.db.repositories.finops import CostLedgerRepo
from app.db.repositories.user import UserRepo
from app.finops.attribution import Agent
from app.finops.service import FinOpsService
from app.finops.tiers import AlertLevel, BudgetScopeKind, BudgetTierPolicy, TieredCap
from app.memory.budget_service import BudgetExceeded, BudgetLimits
from app.providers.types import Usage

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


def _limits(
    *,
    ceiling: float = 1000.0,
    per_session: float = 100.0,
    per_scene: float = 50.0,
    low_floor: float = 30.0,
) -> BudgetLimits:
    return BudgetLimits(
        ceiling_video_s=ceiling,
        per_session_s=per_session,
        per_scene_s=per_scene,
        low_floor_s=low_floor,
        live_video=False,
    )


def _service(
    session: AsyncSession,
    *,
    limits: BudgetLimits | None = None,
    tenant_cap: float = 0.0,
) -> FinOpsService:
    limits = limits or _limits()
    policy = BudgetTierPolicy(
        global_cap=TieredCap(BudgetScopeKind.GLOBAL, limits.ceiling_video_s),
        tenant_cap=TieredCap(
            BudgetScopeKind.TENANT, tenant_cap if tenant_cap > 0 else float("inf")
        ),
        session_cap=TieredCap(BudgetScopeKind.SESSION, limits.per_session_s),
        scene_cap=TieredCap(BudgetScopeKind.SCENE, limits.per_scene_s),
    )
    return FinOpsService(
        budget_repo=BudgetRepo(session),
        cost_repo=CostLedgerRepo(session),
        book_repo=BookRepo(session),
        limits=limits,
        policy=policy,
    )


async def test_reserve_commit_release_passthrough_preserves_contract(
    session: AsyncSession,
) -> None:
    svc = _service(session, limits=_limits(ceiling=20.0))
    assert await svc.budget.remaining() == 20.0
    res = await svc.reserve(8.0)
    assert await svc.budget.remaining() == 12.0
    await svc.commit(res, actual_seconds=5.0)
    assert await svc.budget.remaining() == 15.0
    res2 = await svc.reserve(5.0)
    await svc.release(res2)
    assert await svc.budget.remaining() == 15.0


async def test_tenant_cap_blocks_across_a_tenants_books(session: AsyncSession) -> None:
    users = UserRepo(session)
    books = BookRepo(session)
    tenant = await users.create(email="t@example.com", hashed_password="x")
    book_a = await books.create(title="A", user_id=tenant.id)
    book_b = await books.create(title="B", user_id=tenant.id)

    svc = _service(session, tenant_cap=10.0)

    # Spend 6s on book A and 4s on book B -> tenant total 10s == cap.
    await svc.reserve(6.0, tenant_id=tenant.id, book_id=book_a.id)
    await svc.reserve(4.0, tenant_id=tenant.id, book_id=book_b.id)
    assert await svc.tenant_used_seconds(tenant.id) == pytest.approx(10.0)

    # A further reservation breaches the tenant cap.
    with pytest.raises(BudgetExceeded) as exc:
        await svc.reserve(1.0, tenant_id=tenant.id, book_id=book_a.id)
    assert exc.value.scope == "tenant"
    # The failed reservation did not move the ledger.
    assert await svc.tenant_used_seconds(tenant.id) == pytest.approx(10.0)


async def test_tenant_usage_status_reports_alert_level(session: AsyncSession) -> None:
    users = UserRepo(session)
    books = BookRepo(session)
    tenant = await users.create(email="t2@example.com", hashed_password="x")
    book = await books.create(title="A", user_id=tenant.id)

    svc = _service(session, tenant_cap=100.0)
    await svc.reserve(95.0, tenant_id=tenant.id, book_id=book.id)
    usage = await svc.tenant_usage(tenant.id)
    assert usage.used_s == pytest.approx(95.0)
    assert usage.status.level is AlertLevel.SOFT_CAP


async def test_cost_summary_and_attribution(session: AsyncSession) -> None:
    users = UserRepo(session)
    books = BookRepo(session)
    tenant = await users.create(email="t3@example.com", hashed_password="x")
    book = await books.create(title="A", user_id=tenant.id)

    svc = _service(session)
    # A video render (Generator) and a QA pass (Critic).
    await svc.record_usage_cost(
        Usage(model="wan2.7-i2v", operation="video", video_seconds=5.0),
        Decimal("0.60"),
        tenant_id=tenant.id,
        book_id=book.id,
        shot_id="shot_1",
    )
    await svc.record_usage_cost(
        Usage(model="qwen-vl-max", operation="vl", input_tokens=1000),
        Decimal("0.003"),
        tenant_id=tenant.id,
        book_id=book.id,
        shot_id="shot_1",
    )
    summary = await svc.cost_summary(tenant_id=tenant.id)
    assert summary.cost_usd == Decimal("0.603")
    assert summary.video_seconds == pytest.approx(5.0)
    assert summary.by_agent_usd[Agent.GENERATOR.value] == Decimal("0.60")
    assert summary.by_agent_usd[Agent.CRITIC.value] == Decimal("0.003")
    assert "video" in summary.by_kind_usd
    assert "vl" in summary.by_kind_usd


async def test_reconcile_matches_when_cost_records_committed_seconds(
    session: AsyncSession,
) -> None:
    svc = _service(session)
    book_id = (await BookRepo(session).create(title="R")).id

    # Commit 5s of video against the budget ledger.
    res = await svc.reserve(5.0, book_id=book_id)
    await svc.commit(res, actual_seconds=5.0)
    # Record the same 5s in the cost ledger.
    await svc.record_usage_cost(
        Usage(model="wan2.7-i2v", operation="video", video_seconds=5.0),
        Decimal("0.60"),
        book_id=book_id,
    )
    recon = await svc.reconcile_video_seconds(book_id=book_id)
    assert recon.reconciled
    assert recon.drift_s == pytest.approx(0.0)


async def test_reconcile_flags_drift_when_cost_row_missing(session: AsyncSession) -> None:
    svc = _service(session)
    book_id = (await BookRepo(session).create(title="R2")).id
    res = await svc.reserve(5.0, book_id=book_id)
    await svc.commit(res, actual_seconds=5.0)
    # No cost row recorded -> cost ledger says 0, budget says 5 -> drift -5.
    recon = await svc.reconcile_video_seconds(book_id=book_id)
    assert not recon.reconciled
    assert recon.drift_s == pytest.approx(-5.0)


async def test_used_by_scope_and_cap_statuses(session: AsyncSession) -> None:
    users = UserRepo(session)
    books = BookRepo(session)
    tenant = await users.create(email="t4@example.com", hashed_password="x")
    book = await books.create(title="A", user_id=tenant.id)
    from app.db.repositories.session import SessionRepo

    await SessionRepo(session).upsert(session_id="sess_1", book_id=book.id)

    svc = _service(session, tenant_cap=200.0)
    await svc.reserve(
        40.0, tenant_id=tenant.id, book_id=book.id, session_id="sess_1", scene_id="sc1"
    )

    used = await svc.used_by_scope(tenant_id=tenant.id, session_id="sess_1", scene_id="sc1")
    assert used[BudgetScopeKind.GLOBAL] == pytest.approx(40.0)
    assert used[BudgetScopeKind.TENANT] == pytest.approx(40.0)
    assert used[BudgetScopeKind.SESSION] == pytest.approx(40.0)
    assert used[BudgetScopeKind.SCENE] == pytest.approx(40.0)

    statuses = await svc.cap_statuses(tenant_id=tenant.id, session_id="sess_1", scene_id="sc1")
    scopes = {s.scope for s in statuses}
    assert scopes == {
        BudgetScopeKind.GLOBAL,
        BudgetScopeKind.TENANT,
        BudgetScopeKind.SESSION,
        BudgetScopeKind.SCENE,
    }
    # The scene cap (50s) is the tightest -> the binding scope.
    binding = BudgetTierPolicy.binding_scope(statuses)
    assert binding is not None and binding.scope is BudgetScopeKind.SCENE
