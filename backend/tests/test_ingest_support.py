"""Shared, network-free helpers for the Phase A ingest tests (no tests of its own).

The DB-backed tests run against a throwaway pgvector Postgres and SKIP cleanly
when ``KINORA_TEST_DATABASE_URL`` is unset. Object storage is a fast in-memory
double (:class:`MemoryBlobStore`) so the storage round-trip is exercised without
MinIO; the real :class:`app.storage.object_store.ObjectStore` is covered
separately by ``test_object_store.py`` + the live smoke. Providers are the REAL
aggregate with their high-level methods monkeypatched (the suite-wide pattern).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import fitz  # PyMuPDF
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.providers import Providers, create_providers

EMBED_DIM = 1152

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def one_hot(data: bytes) -> list[float]:
    """Deterministic 1152-d one-hot vector keyed by content (cosine 1.0 vs self)."""
    axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % EMBED_DIM
    vector = [0.0] * EMBED_DIM
    vector[axis] = 1.0
    return vector


class MemoryBlobStore:
    """In-memory ``BlobStore`` double (put/get/exists/presign), no network."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.data[key] = bytes(data)

    def get_bytes(self, key: str) -> bytes:
        if key not in self.data:
            raise KeyError(key)
        return self.data[key]

    def exists(self, key: str) -> bool:
        return key in self.data

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"memory://{key}"

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class FakeEmbedder:
    """Deterministic one-hot embedder (a network-free ``Embedder``)."""

    def __init__(self) -> None:
        self.image_calls = 0
        self.text_calls = 0

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        self.image_calls += 1
        return [one_hot(b) for b in images]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.text_calls += 1
        return [one_hot(t.encode("utf-8")) for t in texts]


def build_test_pdf(
    pages: list[str],
    *,
    draw_shape: bool = True,
    width: float = 612.0,
    height: float = 792.0,
    fontsize: int = 14,
) -> bytes:
    """Build a real multi-page PDF with text and a drawn shape (pseudo-illustration)."""
    doc = fitz.open()
    try:
        for body in pages:
            page = doc.new_page(width=width, height=height)
            page.insert_textbox(
                fitz.Rect(72, 72, width - 72, height / 2),
                body,
                fontsize=fontsize,
                fontname="helv",
            )
            if draw_shape:
                page.draw_circle(
                    fitz.Point(width / 2, height * 0.72),
                    60,
                    color=(0, 0, 1),
                    fill=(0.6, 0.8, 1.0),
                )
        data: bytes = doc.tobytes()
        return data
    finally:
        doc.close()


#: A real 1x1 PNG (the smallest valid PNG) used as an EPUB cover in tests.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def build_test_epub(
    chapters: list[str],
    *,
    title: str = "The Fox and the Owl",
    author: str = "A. Author",
    cover_png: bytes | None = TINY_PNG,
    epub3_cover: bool = True,
) -> bytes:
    """Build a minimal, valid EPUB (a ZIP container) with text + an optional cover.

    Mirrors :func:`build_test_pdf`: no network, no on-disk fixture — the bytes are
    assembled in memory. Each chapter becomes one XHTML document in the spine.
    ``epub3_cover`` selects the EPUB 3 ``properties="cover-image"`` form; set it
    ``False`` to emit the EPUB 2 ``<meta name="cover">`` pointer instead.
    """
    manifest_items: list[str] = []
    spine_items: list[str] = []
    meta_cover = ""
    chapter_files: dict[str, str] = {}

    if cover_png is not None:
        if epub3_cover:
            manifest_items.append(
                '<item id="cover-img" href="cover.png" media-type="image/png" '
                'properties="cover-image"/>'
            )
        else:
            manifest_items.append(
                '<item id="cover-img" href="cover.png" media-type="image/png"/>'
            )
            meta_cover = '<meta name="cover" content="cover-img"/>'

    for index, body in enumerate(chapters, start=1):
        href = f"chap{index}.xhtml"
        item_id = f"chap{index}"
        manifest_items.append(
            f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{item_id}"/>')
        chapter_files[f"OEBPS/{href}"] = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>Chapter {index}</title></head><body>"
            f"<h1>Chapter {index}</h1><p>{body}</p></body></html>"
        )

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:kinora-test</dc:identifier>'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
        f"<dc:language>en</dc:language>{meta_cover}</metadata>"
        f'<manifest>{"".join(manifest_items)}</manifest>'
        f'<spine>{"".join(spine_items)}</spine></package>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # The mimetype entry must be first and stored (uncompressed) per the OCF spec.
        zf.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED
        )
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        if cover_png is not None:
            zf.writestr("OEBPS/cover.png", cover_png)
        for path, html in chapter_files.items():
            zf.writestr(path, html)
    return buf.getvalue()


def make_providers() -> Providers:
    """A REAL provider aggregate with a dummy key (methods are monkeypatched)."""
    return create_providers(Settings(dashscope_api_key="test"))


@pytest_asyncio.fixture
async def providers() -> AsyncIterator[Providers]:
    """Provider aggregate closed on teardown (no network is ever made)."""
    aggregate = make_providers()
    try:
        yield aggregate
    finally:
        await aggregate.aclose()


def new_engine() -> AsyncEngine:
    """A fresh engine on the throwaway test DB (NullPool: no cross-test sharing)."""
    assert _DB_URL is not None
    return create_async_engine(_DB_URL, poolclass=NullPool)


async def create_schema(engine: AsyncEngine) -> None:
    """Ensure the pgvector extension + all tables exist on the test DB."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)


def committing_session_factory(engine: AsyncEngine) -> SessionFactory:
    """A ``get_session``-shaped factory that COMMITS on clean exit.

    The ingest service runs each phase in its own unit of work and relies on the
    prior phase's commit being visible, so the multi-session service test needs a
    committing factory (not the rollback-on-teardown single session below).
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    return _factory


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A single isolated session; rolls back all writes on teardown."""
    engine = new_engine()
    await create_schema(engine)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()
