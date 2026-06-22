"""Canon vault export — the inspectable markdown bible (§8.1).

Real Postgres integration (SKIP without ``KINORA_TEST_DATABASE_URL``). Writes to
a real S3/MinIO object store when ``KINORA_TEST_S3_ENDPOINT`` is set, otherwise
to an in-memory blob-store double that satisfies the same ``BlobStore`` protocol
the production ``ObjectStore`` does.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.models.enums import EntityType
from app.db.repositories.book import BookRepo
from app.memory.canon_service import CanonService
from app.memory.canon_vault import CanonVault
from app.memory.interfaces import BlobStore

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

_DIM = 1152


class FakeEmbedder:
    """Deterministic one-hot embedder (test double for ``Embedder``)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one_hot(t.encode("utf-8")) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._one_hot(b) for b in images]

    @staticmethod
    def _one_hot(data: bytes) -> list[float]:
        axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % _DIM
        vector = [0.0] * _DIM
        vector[axis] = 1.0
        return vector


class FakeBlobStore:
    """In-memory blob store satisfying the ``BlobStore`` protocol."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.data[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self.data[key]

    def exists(self, key: str) -> bool:
        return key in self.data

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"https://example.test/{key}"


def _make_store() -> BlobStore:
    endpoint = os.environ.get("KINORA_TEST_S3_ENDPOINT")
    if endpoint:
        from app.storage.object_store import ObjectStore

        store = ObjectStore(
            endpoint_url=endpoint,
            region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
            access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
            secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
            bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora"),
        )
        store.ensure_bucket()
        return store
    return FakeBlobStore()


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


async def test_vault_export_writes_readable_markdown(session: AsyncSession) -> None:
    books = BookRepo(session)
    canon = CanonService(session, embedder=FakeEmbedder())

    book = await books.create(title="Vault Tale")
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_hero",
        entity_type=EntityType.CHARACTER,
        name="Hero",
        valid_from_beat=1,
        appearance={"description": "a brave knight", "locked": True},
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="loc_castle",
        entity_type=EntityType.LOCATION,
        name="Castle",
        valid_from_beat=1,
    )
    state_id = await canon.assert_state(
        book_id=book.id,
        subject_entity_key="char_hero",
        predicate="possesses",
        object_value="prop_sword",
        valid_from_beat=1,
    )
    await canon.retire_state(state_id, valid_to_beat=20)

    store = _make_store()
    vault = CanonVault(session, blob_store=store)
    export = await vault.export(book.id)

    blob = "\n".join(export.files.values())
    # The characters and the (retired) continuity fact are all present.
    assert "Hero" in blob
    assert "Castle" in blob
    assert "possesses" in blob
    assert "prop_sword" in blob
    assert "retired" in blob

    # And it was actually written to object storage.
    assert export.index_key in export.keys
    assert store.exists(export.index_key)
    written = store.get_bytes(export.index_key).decode("utf-8")
    assert "Vault Tale" in written
    assert "Hero" in written
