"""Content-hash shot cache — re-reads cost zero, edits re-render surgically (§8.7).

Real Postgres integration (SKIP without ``KINORA_TEST_DATABASE_URL``).
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
from app.db.repositories.shot import ShotCacheRepo
from app.memory.cache_service import CacheService

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


def test_reference_set_hash_is_order_independent() -> None:
    h1 = CacheService.reference_set_hash(["char_elsa@v3", "loc_window@v1"])
    h2 = CacheService.reference_set_hash(["loc_window@v1", "char_elsa@v3"])
    h3 = CacheService.reference_set_hash(["char_elsa@v3", "loc_window@v2"])
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha1:")


async def test_cache_hit_returns_zero_video_seconds(session: AsyncSession) -> None:
    book = await BookRepo(session).create(title="Cache")
    cache = CacheService(cache=ShotCacheRepo(session))

    miss = await cache.check_or_miss(
        book_id=book.id,
        beat_id="beat_1",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=88123,
        reference_image_ids=["char_elsa@v3"],
    )
    assert miss.hit is False
    assert miss.video_seconds == 0.0

    await cache.put(
        shot_hash=miss.shot_hash,
        book_id=book.id,
        clip_key="clips/elsa.mp4",
        last_frame_key="lastframes/elsa.png",
        video_seconds=5.0,
    )

    hit = await cache.check_or_miss(
        book_id=book.id,
        beat_id="beat_1",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=88123,
        reference_image_ids=["char_elsa@v3"],
    )
    assert hit.hit is True
    assert hit.shot_hash == miss.shot_hash
    assert hit.clip_key == "clips/elsa.mp4"
    assert hit.video_seconds == 0.0


async def test_changed_inputs_change_the_hash(session: AsyncSession) -> None:
    book = await BookRepo(session).create(title="Surgical")
    cache = CacheService(cache=ShotCacheRepo(session))

    base = await cache.check_or_miss(
        book_id=book.id,
        beat_id="beat_1",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=88123,
        reference_image_ids=["char_elsa@v3"],
    )
    # Different seed → different shot.
    diff_seed = await cache.check_or_miss(
        book_id=book.id,
        beat_id="beat_1",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=99999,
        reference_image_ids=["char_elsa@v3"],
    )
    # Director edit changes the reference set → only this shot re-renders (§8.7).
    diff_refs = await cache.check_or_miss(
        book_id=book.id,
        beat_id="beat_1",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=88123,
        reference_image_ids=["char_elsa@v4"],
    )
    assert len({base.shot_hash, diff_seed.shot_hash, diff_refs.shot_hash}) == 3
    assert base.reference_set_hash != diff_refs.reference_set_hash
