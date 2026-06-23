"""Book endpoint tests — upload validation + ingest trigger, shelf, reads (§5.1/§9.1)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import UploadFile
from httpx import AsyncClient

from app.api.errors import APIError
from app.api.routes import books as books_module
from app.composition import Container
from app.db.base import new_id
from app.db.models.enums import BookStatus, EntityType, ShotStatus
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from tests.conftest import FakeIngestRunner, register_login, tiny_pdf
from tests.test_ingest_support import TINY_PNG, build_test_epub


def _files(
    data: bytes, *, name: str = "tale.pdf", content_type: str = "application/pdf"
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, content_type)}


def _tiny_epub() -> bytes:
    """A minimal valid EPUB (title + author + cover) for upload tests."""
    return build_test_epub(
        ["The fox runs through the forest at dawn. " + ("word " * 30)],
        title="An EPUB Tale",
        author="E. Pub",
    )


async def _poll_status(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    for _ in range(20):
        resp = await client.get(f"/api/books/{book_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()["status"]
        if status == "ready":
            return status
        await asyncio.sleep(0.05)
    return status


async def test_upload_accepts_real_pdf_and_triggers_ingest(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/books", headers=auth_headers, files=_files(tiny_pdf()), data={"title": "My Tale"}
    )
    assert resp.status_code == 201, resp.text
    # The upload returns the freshly-created book directly (a bare Book, no envelope).
    book = resp.json()
    assert book["title"] == "My Tale"
    assert book["status"] in {"importing", "ready"}
    book_id = book["id"]

    # The fake ingest runner records the trigger and marks the book ready.
    runner = container.ingest_runner
    assert isinstance(runner, FakeIngestRunner)
    status = await _poll_status(api_client, auth_headers, book_id)
    assert status == "ready"
    assert runner.calls == [book_id]

    shelf = await api_client.get("/api/books", headers=auth_headers)
    assert shelf.status_code == 200
    # The shelf is a bare array of books (no envelope).
    assert any(b["id"] == book_id for b in shelf.json())


async def test_upload_rejects_non_pdf(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    wrong_type = await api_client.post(
        "/api/books", headers=auth_headers, files=_files(b"hello", content_type="text/plain")
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["type"] == "unsupported_media_type"

    bad_magic = await api_client.post(
        "/api/books", headers=auth_headers, files=_files(b"not really a pdf at all")
    )
    assert bad_magic.status_code == 400
    # Neither a PDF nor an EPUB by content magic.
    assert bad_magic.json()["error"]["type"] == "unsupported_file"


async def test_upload_accepts_epub_and_triggers_ingest(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """An EPUB upload is accepted, normalised, and ingested like a PDF (§5.1)."""
    resp = await api_client.post(
        "/api/books",
        headers=auth_headers,
        files=_files(_tiny_epub(), name="tale.epub", content_type="application/epub+zip"),
    )
    assert resp.status_code == 201, resp.text
    book = resp.json()
    # Title/author fall back to the EPUB's own metadata when not supplied.
    assert book["title"] == "An EPUB Tale"
    assert book["author"] == "E. Pub"
    book_id = book["id"]

    runner = container.ingest_runner
    assert isinstance(runner, FakeIngestRunner)
    status = await _poll_status(api_client, auth_headers, book_id)
    assert status == "ready"
    assert runner.calls == [book_id]

    # The normalised PDF + the original EPUB + the declared cover were all stored.
    store = container.object_store
    from app.storage.object_store import keys

    assert store.exists(keys.pdf(book_id))
    assert store.get_bytes(keys.pdf(book_id))[:5] == b"%PDF-"
    assert store.exists(keys.epub(book_id))
    assert store.exists(keys.cover(book_id))
    assert store.get_bytes(keys.cover(book_id)) == TINY_PNG


async def test_upload_epub_explicit_title_overrides_metadata(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A caller-supplied title/author wins over the EPUB's embedded metadata."""
    resp = await api_client.post(
        "/api/books",
        headers=auth_headers,
        files=_files(_tiny_epub(), name="tale.epub", content_type="application/epub+zip"),
        data={"title": "My Override", "author": "Me"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "My Override"
    assert body["author"] == "Me"


async def test_upload_rejects_corrupt_epub(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A file that sniffs as an EPUB but is unreadable is rejected (not 500)."""
    import io
    import zipfile

    # A ZIP with the EPUB mimetype but no OPF/container — passes magic, fails parse.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
    resp = await api_client.post(
        "/api/books",
        headers=auth_headers,
        files=_files(buf.getvalue(), name="broken.epub", content_type="application/epub+zip"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_epub"


async def test_upload_rejects_oversize(
    api_client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(books_module, "MAX_PDF_BYTES", 32)
    resp = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "file_too_large"


async def test_get_page_returns_text_and_boxes(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["id"]
    async with container.session_factory() as session:
        await PageRepo(session).create(
            book_id=book_id,
            page_number=1,
            image_key=f"pages/{book_id}/1.png",
            text="Once upon a time",
            word_boxes=[{"word_index": 0, "text": "Once", "bbox": [0.1, 0.1, 0.2, 0.05]}],
        )

    resp = await api_client.get(f"/api/books/{book_id}/pages/1", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Once upon a time"
    assert body["word_boxes"][0]["text"] == "Once"
    assert body["image_url"] and body["image_url"].startswith("http")

    missing = await api_client.get(f"/api/books/{book_id}/pages/99", headers=auth_headers)
    assert missing.status_code == 404


async def test_get_canon_vault(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["id"]
    async with container.session_factory() as session:
        assert container.embedder is not None
        canon = CanonService(
            session, embedder=container.embedder, blob_store=container.object_store
        )
        await canon.upsert_entity(
            book_id=book_id,
            entity_key="char_hero",
            entity_type=EntityType.CHARACTER,
            name="Brave Hero",
            valid_from_beat=1,
        )

    resp = await api_client.get(f"/api/books/{book_id}/canon", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    # The canon graph projects the current-version entities the editor renders,
    # keyed by entity_key (what the canon-edit call targets), plus the markdown vault.
    entities = body["entities"]
    hero = next(e for e in entities if e["id"] == "char_hero")
    assert hero["name"] == "Brave Hero"
    assert hero["type"] == "character"
    assert hero["version"] == 1
    assert "char_hero" in body["markdown"]
    assert "Brave Hero" in body["markdown"]


async def test_list_shots(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["id"]
    async with container.session_factory() as session:
        await ShotRepo(session).create(
            id="shot_1",
            book_id=book_id,
            beat_id="beat_1",
            scene_id="scene_1",
            source_span={"page": 1, "para": 1, "word_range": [0, 9]},
            status=ShotStatus.ACCEPTED,
            render_mode="reference_to_video",
            duration_s=5.0,
            reference_image_ids=["char_hero@v1"],
            qa={"verdict": "pass", "ccs": 0.9},
            output={"clip_key": f"clips/{book_id}/shot_1.mp4"},
        )

    resp = await api_client.get(f"/api/books/{book_id}/shots", headers=auth_headers)
    assert resp.status_code == 200
    # The shot timeline is a bare array; each shot carries its source_span so the
    # client's SyncEngine can sort/seek by reading position.
    shots = resp.json()
    assert len(shots) == 1
    assert shots[0]["shot_id"] == "shot_1"
    assert shots[0]["status"] == "accepted"
    assert shots[0]["source_span"] == {"page": 1, "para": 1, "word_range": [0, 9]}
    assert shots[0]["qa"]["verdict"] == "pass"
    assert shots[0]["clip_url"] and shots[0]["clip_url"].startswith("http")


async def test_book_not_owned_is_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["id"]

    other = await register_login(api_client, "intruder@example.com")
    resp = await api_client.get(f"/api/books/{book_id}", headers=other)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "book_not_found"


async def test_book_with_null_owner_is_inaccessible(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """Fix 10: a book with no durable owner (user_id NULL) belongs to nobody."""
    book_id = new_id()
    async with container.session_factory() as session:
        # Created with no user_id — a legacy/orphaned row.
        await BookRepo(session).create(title="Orphan", book_id=book_id, status=BookStatus.READY)
    resp = await api_client.get(f"/api/books/{book_id}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "book_not_found"


async def test_upload_sets_durable_owner_and_lists_it(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """Fix 10: upload persists books.user_id; the shelf reads it (no Redis needed)."""
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["id"]
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    assert book is not None and book.user_id is not None
    shelf = await api_client.get("/api/books", headers=auth_headers)
    assert any(b["id"] == book_id for b in shelf.json())


# --------------------------------------------------------------------------- #
# Fix 6: upload DoS + per-user ingest quota
# --------------------------------------------------------------------------- #


async def test_read_capped_aborts_without_buffering_whole_body() -> None:
    """The streaming reader aborts the instant the cap is crossed (no full buffer)."""
    chunk = books_module._READ_CHUNK

    class _BigUpload:
        def __init__(self, total_chunks: int) -> None:
            self._left = total_chunks
            self.served = 0

        async def read(self, size: int = -1) -> bytes:
            if self._left <= 0:
                return b""
            self._left -= 1
            self.served += 1
            return b"\x00" * chunk

    fake = _BigUpload(total_chunks=100)  # would be 100 MiB if read in full
    cap = 2 * chunk  # 2 MiB
    with pytest.raises(APIError) as excinfo:
        await books_module._read_capped(cast("UploadFile", fake), cap)
    assert excinfo.value.status == 413
    # Aborted after crossing the cap — only ~3 chunks pulled, not all 100.
    assert fake.served <= 3


async def test_upload_rejects_too_many_pages(
    api_client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(books_module, "MAX_INGEST_PAGES", 0)
    resp = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "too_many_pages"


async def test_upload_enforces_max_books_per_user(
    api_client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(books_module, "MAX_BOOKS_PER_USER", 1)
    first = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    assert first.status_code == 201
    second = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "book_quota_exceeded"
