"""Book endpoint tests — upload validation + ingest trigger, shelf, reads (§5.1/§9.1)."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient

from app.api.routes import books as books_module
from app.composition import Container
from app.db.models.enums import EntityType, ShotStatus
from app.db.repositories.book import PageRepo
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from tests.conftest import register_login, tiny_pdf


def _files(data: bytes, *, name: str = "tale.pdf", content_type: str = "application/pdf"):
    return {"file": (name, data, content_type)}


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
    body = resp.json()
    assert body["ingest_started"] is True
    book = body["book"]
    assert book["title"] == "My Tale"
    assert book["status"] in {"importing", "ready"}
    book_id = book["id"]

    # The fake ingest runner records the trigger and marks the book ready.
    assert isinstance(container.ingest_runner, object)
    status = await _poll_status(api_client, auth_headers, book_id)
    assert status == "ready"
    assert container.ingest_runner.calls == [book_id]  # type: ignore[attr-defined]

    shelf = await api_client.get("/api/books", headers=auth_headers)
    assert shelf.status_code == 200
    assert any(b["id"] == book_id for b in shelf.json()["books"])


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
    assert bad_magic.json()["error"]["type"] == "invalid_pdf"


async def test_upload_rejects_oversize(
    api_client: AsyncClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr(books_module, "MAX_PDF_BYTES", 32)
    resp = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "file_too_large"


async def test_get_page_returns_text_and_boxes(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["book"]["id"]
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
    book_id = up.json()["book"]["id"]
    async with container.session_factory() as session:
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
    assert body["index_url"].startswith("http")
    blob = "\n".join(body["markdown"].values())
    assert "char_hero" in blob
    assert "Brave Hero" in blob


async def test_list_shots(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["book"]["id"]
    async with container.session_factory() as session:
        await ShotRepo(session).create(
            id="shot_1",
            book_id=book_id,
            beat_id="beat_1",
            scene_id="scene_1",
            status=ShotStatus.ACCEPTED,
            render_mode="reference_to_video",
            duration_s=5.0,
            reference_image_ids=["char_hero@v1"],
            qa={"verdict": "pass", "ccs": 0.9},
            output={"clip_key": f"clips/{book_id}/shot_1.mp4"},
        )

    resp = await api_client.get(f"/api/books/{book_id}/shots", headers=auth_headers)
    assert resp.status_code == 200
    shots = resp.json()["shots"]
    assert len(shots) == 1
    assert shots[0]["shot_id"] == "shot_1"
    assert shots[0]["status"] == "accepted"
    assert shots[0]["clip_url"] and shots[0]["clip_url"].startswith("http")


async def test_book_not_owned_is_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    up = await api_client.post("/api/books", headers=auth_headers, files=_files(tiny_pdf()))
    book_id = up.json()["book"]["id"]

    other = await register_login(api_client, "intruder@example.com")
    resp = await api_client.get(f"/api/books/{book_id}", headers=other)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "book_not_found"
