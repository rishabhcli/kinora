"""DB-backed test for the lifecycle GC sweep (isolated DB; skips when unset)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401
from app.db.base import Base
from app.media.hashing import sha256_hex
from app.media.kinds import MediaAssetKind
from app.media.lifecycle import sweep_expired, verify_integrity
from app.media.repository import MediaAssetRepo
from app.media.store import MediaStore
from app.media.testing import FakeMediaStore

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping media GC DB tests"
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


async def test_sweep_collects_expired_orphaned_derived(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    backend = FakeMediaStore()
    store = MediaStore(backend)
    now = datetime.now(UTC)
    past = now - timedelta(hours=1)

    # An expired, orphaned poster with a real blob → collected.
    backend.put_bytes("m/poster.png", b"poster", "image/png")
    collectible = await repo.create(
        storage_key="m/poster.png",
        kind=MediaAssetKind.POSTER,
        size_bytes=6,
        expires_at=past,
    )
    # An expired but referenced sprite → kept.
    backend.put_bytes("m/sprite.png", b"sprite", "image/png")
    await repo.create(
        storage_key="m/sprite.png",
        kind=MediaAssetKind.SPRITE,
        expires_at=past,
        ref_count=1,
    )
    # An expired CLIP (primary kind) → never collected by the derived sweep.
    backend.put_bytes("m/clip.mp4", b"clip", "video/mp4")
    clip = await repo.create(
        storage_key="m/clip.mp4", kind=MediaAssetKind.CLIP, expires_at=past
    )

    result = await sweep_expired(repo, store, now=now)
    assert result.collected == 1
    assert result.bytes_freed == 6
    assert "m/poster.png" in result.keys

    # the poster blob + row are gone; the others remain
    assert "m/poster.png" not in backend
    assert await repo.get(collectible.id) is None
    assert "m/clip.mp4" in backend
    assert await repo.get(clip.id) is not None


async def test_sweep_is_idempotent_when_blob_missing(session: AsyncSession) -> None:
    repo = MediaAssetRepo(session)
    store = MediaStore(FakeMediaStore())  # blob never uploaded
    now = datetime.now(UTC)
    await repo.create(
        storage_key="m/ghost.png",
        kind=MediaAssetKind.THUMBNAIL,
        expires_at=now - timedelta(minutes=5),
    )
    result = await sweep_expired(repo, store, now=now)
    # still collects the row even though the blob delete was a no-op
    assert result.collected == 1


async def test_verify_integrity_detects_missing_and_corrupt(session: AsyncSession) -> None:
    from app.db.repositories.book import BookRepo

    book = await BookRepo(session).create(title="Integrity Book")
    repo = MediaAssetRepo(session)
    backend = FakeMediaStore()
    store = MediaStore(backend)

    # healthy: stored bytes match recorded hash
    good = b"good-bytes"
    backend.put_bytes("m/good.mp4", good, "video/mp4")
    await repo.create(
        storage_key="m/good.mp4",
        kind=MediaAssetKind.CLIP,
        content_hash=sha256_hex(good),
        book_id=book.id,
    )
    # corrupt: stored bytes differ from recorded hash
    backend.put_bytes("m/bad.mp4", b"tampered", "video/mp4")
    await repo.create(
        storage_key="m/bad.mp4",
        kind=MediaAssetKind.CLIP,
        content_hash=sha256_hex(b"original"),
        book_id=book.id,
    )
    # missing: hash recorded but no blob in the store
    await repo.create(
        storage_key="m/gone.mp4",
        kind=MediaAssetKind.CLIP,
        content_hash=sha256_hex(b"vanished"),
        book_id=book.id,
    )
    # no hash recorded → skipped (not counted)
    await repo.create(
        storage_key="m/nohash.mp4", kind=MediaAssetKind.CLIP, book_id=book.id
    )

    report = await verify_integrity(repo, store, book_id=book.id)
    assert report.checked == 3  # nohash skipped
    assert report.ok == 1
    assert report.missing == ("m/gone.mp4",)
    assert report.corrupt == ("m/bad.mp4",)
    assert report.healthy is False
