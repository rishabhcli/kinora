"""DB-backed tests for the media_assets repository.

Runs against an isolated throwaway Postgres (``KINORA_TEST_DATABASE_URL``, e.g.
``kinora_media_test`` on :5433) and SKIPS cleanly when unset. Each test rolls
back on teardown — repositories only flush, never commit — so writes never
escape the test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.media.kinds import MediaAssetKind
from app.media.metadata import AssetMetadata
from app.media.repository import MediaAssetRepo

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping media DB tests"
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


def _meta(key: str, **kw: Any) -> AssetMetadata:
    base: dict[str, Any] = {
        "storage_key": key,
        "kind": MediaAssetKind.CLIP,
        "content_type": "video/mp4",
    }
    base.update(kw)
    return AssetMetadata(**base)


async def test_create_and_get(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    asset = await repo.create(
        storage_key="media/by-hash/aa/bb/x.mp4",
        kind=MediaAssetKind.CLIP,
        content_hash="a" * 64,
        content_type="video/mp4",
        size_bytes=1234,
        width=720,
        height=1280,
        duration_s=5.0,
    )
    fetched = await repo.get(asset.id)
    assert fetched is not None
    assert fetched.storage_key == "media/by-hash/aa/bb/x.mp4"
    assert fetched.width == 720
    assert fetched.ref_count == 0


async def test_register_is_idempotent_by_key(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    m = _meta("media/by-hash/cc/dd/y.mp4", content_hash="b" * 64, size_bytes=10)
    a1 = await repo.register(m)
    a2 = await repo.register(m.model_copy(update={"size_bytes": 20}))
    assert a1.id == a2.id  # same key → upsert
    assert a2.size_bytes == 20


async def test_register_merges_extra_meta(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    m = _meta("media/k.mp4", meta={"parent": "clip_1"})
    asset = await repo.register(m, extra_meta={"variants": ["1280p"]})
    assert asset.meta == {"parent": "clip_1", "variants": ["1280p"]}


async def test_find_by_hash(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    await repo.create(
        storage_key="media/z.png", kind=MediaAssetKind.POSTER, content_hash="c" * 64
    )
    found = await repo.find_by_hash("c" * 64)
    assert found is not None
    assert found.kind == MediaAssetKind.POSTER
    # kind filter
    assert await repo.find_by_hash("c" * 64, kind=MediaAssetKind.CLIP) is None
    assert await repo.find_by_hash("missing" * 8) is None


async def test_incr_ref_clamps_at_zero(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    asset = await repo.create(storage_key="media/r.mp4", ref_count=1)
    await repo.incr_ref(asset.id, by=2)
    refreshed = await repo.get(asset.id)
    assert refreshed is not None and refreshed.ref_count == 3
    await repo.incr_ref(asset.id, by=-10)
    refreshed = await repo.get(asset.id)
    assert refreshed is not None and refreshed.ref_count == 0


async def test_list_for_book_and_kind(session: AsyncSession) -> None:
    from app.db.repositories.book import BookRepo

    book = await BookRepo(session).create(title="Media Book")
    repo = MediaAssetRepo(session)
    await repo.create(storage_key="m/clip.mp4", kind=MediaAssetKind.CLIP, book_id=book.id)
    await repo.create(storage_key="m/poster.png", kind=MediaAssetKind.POSTER, book_id=book.id)
    all_for_book = await repo.list_for_book(book.id)
    assert len(all_for_book) == 2
    posters = await repo.list_for_book(book.id, kind=MediaAssetKind.POSTER)
    assert len(posters) == 1
    assert posters[0].kind == MediaAssetKind.POSTER


async def test_list_expired_only_orphaned_and_due(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    # due + orphaned → collectible
    due = await repo.create(
        storage_key="m/due.png", kind=MediaAssetKind.THUMBNAIL, expires_at=past
    )
    # due but referenced → kept
    await repo.create(
        storage_key="m/ref.png", kind=MediaAssetKind.THUMBNAIL, expires_at=past, ref_count=1
    )
    # not yet due → kept
    await repo.create(
        storage_key="m/future.png", kind=MediaAssetKind.THUMBNAIL, expires_at=future
    )
    # no expiry → kept
    await repo.create(storage_key="m/keep.png", kind=MediaAssetKind.THUMBNAIL)

    expired = await repo.list_expired(now=now)
    ids = {a.id for a in expired}
    assert due.id in ids
    assert len(expired) == 1

    # kind restriction excludes other kinds
    none_for_clips = await repo.list_expired(now=now, kinds=[MediaAssetKind.CLIP])
    assert none_for_clips == []


async def test_delete_and_total_bytes(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    a = await repo.create(storage_key="m/a.mp4", size_bytes=100)
    await repo.create(storage_key="m/b.mp4", size_bytes=200)
    assert await repo.total_bytes() == 300
    assert await repo.delete(a.id) is True
    assert await repo.delete("nonexistent") is False
    assert await repo.total_bytes() == 200


async def test_set_expiry(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    asset = await repo.create(storage_key="m/exp.mp4")
    when = datetime.now(UTC) + timedelta(days=7)
    await repo.set_expiry(asset.id, when)
    refreshed = await repo.get(asset.id)
    assert refreshed is not None and refreshed.expires_at is not None
    await repo.set_expiry(asset.id, None)
    refreshed = await repo.get(asset.id)
    assert refreshed is not None and refreshed.expires_at is None
