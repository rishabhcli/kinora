"""Cover field + ``GET /books/{id}/cover`` — Agent 05 (library/covers, §5.1).

A book grows a real ``cover_key`` (object-store key) projected as a presigned
``cover_url`` on every ``BookResponse``; the dedicated cover endpoint 302-redirects
to that presigned URL (authed + ownership-checked) so native shells / ``<img>``
have one stable accessor, and 404s when a book has no cover yet.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from httpx import AsyncClient

from app.composition import Container
from app.db.repositories.book import BookRepo
from app.storage.object_store import keys
from tests.conftest import requires_infra, seed_owned_book

pytestmark = requires_infra


def _png(color: tuple[int, int, int] = (40, 40, 60), size: tuple[int, int] = (8, 12)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


async def _give_cover(container: Container, book_id: str) -> str:
    key = keys.cover(book_id)
    container.object_store.put_bytes(key, _png(), "image/png")
    async with container.session_factory() as session:
        await BookRepo(session).set_cover_key(book_id, key)
    return key


async def test_book_response_exposes_cover_url(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Covered")
    await _give_cover(container, book_id)
    resp = await api_client.get("/api/books", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    book = next(b for b in resp.json() if b["id"] == book_id)
    assert book["cover_url"], "cover_url should be a presigned URL"
    assert keys.cover(book_id) in book["cover_url"]


async def test_book_without_cover_has_null_cover_url(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Bare")
    resp = await api_client.get(f"/api/books/{book_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["cover_url"] is None


async def test_cover_endpoint_redirects_to_presigned(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Redirects")
    await _give_cover(container, book_id)
    resp = await api_client.get(
        f"/api/books/{book_id}/cover", headers=auth_headers, follow_redirects=False
    )
    assert resp.status_code in (302, 307), resp.text
    assert keys.cover(book_id) in resp.headers["location"]


async def test_cover_endpoint_404_when_no_cover(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="NoCover")
    resp = await api_client.get(
        f"/api/books/{book_id}/cover", headers=auth_headers, follow_redirects=False
    )
    assert resp.status_code == 404, resp.text


async def test_cover_endpoint_enforces_ownership(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Any],
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Mine")
    await _give_cover(container, book_id)
    other = await make_user("intruder@example.com")
    resp = await api_client.get(
        f"/api/books/{book_id}/cover", headers=other, follow_redirects=False
    )
    assert resp.status_code == 404, resp.text
