"""Budget ledger semantics — the hard video-seconds cap (§11.1).

Real Postgres integration (SKIP without ``KINORA_TEST_DATABASE_URL``). Uses small
caps so the ceiling/per-session/per-scene behaviour is exercised cheaply. Each
test rolls back on teardown.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.repositories.book import BookRepo
from app.db.repositories.budget import BudgetRepo
from app.db.repositories.session import SessionRepo
from app.memory.budget_service import BudgetExceeded, BudgetLimits, BudgetService

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


def _budget(
    session: AsyncSession,
    *,
    ceiling: float = 20.0,
    per_session: float = 10.0,
    per_scene: float = 6.0,
    low_floor: float = 5.0,
    live_video: bool = False,
) -> BudgetService:
    limits = BudgetLimits(
        ceiling_video_s=ceiling,
        per_session_s=per_session,
        per_scene_s=per_scene,
        low_floor_s=low_floor,
        live_video=live_video,
    )
    return BudgetService(repo=BudgetRepo(session), limits=limits)


async def test_global_ceiling(session: AsyncSession) -> None:
    budget = _budget(session, ceiling=20.0)
    assert await budget.remaining() == 20.0

    await budget.reserve(8.0)
    await budget.reserve(8.0)
    assert await budget.remaining() == 4.0

    with pytest.raises(BudgetExceeded) as exc:
        await budget.reserve(5.0)
    assert exc.value.scope == "ceiling"
    # The failed reservation did not change the ledger.
    assert await budget.remaining() == 4.0


async def test_per_session_and_per_scene_caps(session: AsyncSession) -> None:
    books = BookRepo(session)
    sessions = SessionRepo(session)
    book = await books.create(title="Caps")
    await sessions.upsert(session_id="sess_x", book_id=book.id)

    budget = _budget(session, per_session=10.0, per_scene=6.0)

    # Per-scene cap (scene_id is a plain string; no session scope here).
    await budget.reserve(6.0, scene_id="scene_a")
    with pytest.raises(BudgetExceeded) as scene_exc:
        await budget.reserve(1.0, scene_id="scene_a")
    assert scene_exc.value.scope == "scene"

    # Per-session cap, spread across distinct scenes so the scene cap is not hit.
    await budget.reserve(5.0, session_id="sess_x", scene_id="scene_b")
    await budget.reserve(5.0, session_id="sess_x", scene_id="scene_c")
    with pytest.raises(BudgetExceeded) as session_exc:
        await budget.reserve(1.0, session_id="sess_x", scene_id="scene_d")
    assert session_exc.value.scope == "session"


async def test_release_restores_and_commit_charges_actual(session: AsyncSession) -> None:
    budget = _budget(session, ceiling=20.0)

    reservation = await budget.reserve(8.0)
    assert await budget.remaining() == 12.0

    # Release returns the earmark.
    await budget.release(reservation)
    assert await budget.remaining() == 20.0

    # Commit charges the *actual* seconds, not the reserved amount.
    reservation2 = await budget.reserve(8.0)
    assert await budget.remaining() == 12.0
    await budget.commit(reservation2, actual_seconds=5.0)
    assert await budget.remaining() == 15.0


async def test_is_low_and_can_render_live(session: AsyncSession) -> None:
    budget = _budget(session, ceiling=20.0, low_floor=5.0, live_video=False)
    assert await budget.is_low() is False
    await budget.reserve(16.0)  # remaining 4.0 < 5.0
    assert await budget.is_low() is True
    assert budget.can_render_live() is False

    live = _budget(session, live_video=True)
    assert live.can_render_live() is True
