"""Async client parity tests. All HTTP mocked via respx; zero live calls."""

from __future__ import annotations

import json

import httpx
import respx

from kinora import AsyncKinoraClient, NotFoundError
from kinora.models import BookResponse

from conftest import BASE_URL, FAST_RETRY

import pytest


def _frame(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@respx.mock
async def test_async_login_and_me() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY) as client:
        respx.post(f"{BASE_URL}/api/auth/login").mock(
            return_value=httpx.Response(200, json={"access_token": "abc", "token_type": "bearer", "expires_in": 3600})
        )
        respx.get(f"{BASE_URL}/api/auth/me").mock(return_value=httpx.Response(200, json={"id": "u1", "email": "a@b.co"}))
        tok = await client.auth.login("a@b.co", "password1")
        assert tok.access_token == "abc"
        user = await client.auth.me()
        assert user.id == "u1"


@respx.mock
async def test_async_books_list() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY, token="tok") as client:
        respx.get(f"{BASE_URL}/api/books").mock(
            return_value=httpx.Response(200, json=[{"id": "b1", "title": "A", "status": "ready"}])
        )
        books = await client.books.list()
        assert isinstance(books[0], BookResponse)
        assert books[0].id == "b1"


@respx.mock
async def test_async_error_mapping() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY, token="tok") as client:
        respx.get(f"{BASE_URL}/api/books/nope").mock(
            return_value=httpx.Response(404, json={"error": {"type": "book_not_found", "message": "no"}})
        )
        with pytest.raises(NotFoundError):
            await client.books.get("nope")


@respx.mock
async def test_async_retry_then_success() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY, token="tok") as client:
        route = respx.get(f"{BASE_URL}/api/books").mock(
            side_effect=[
                httpx.Response(503, json={"error": {"type": "x", "message": "down"}}),
                httpx.Response(200, json=[{"id": "b1", "title": "A", "status": "ready"}]),
            ]
        )
        books = await client.books.list()
        assert len(books) == 1
        assert route.call_count == 2


@respx.mock
async def test_async_iter_events() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY, token="tok") as client:
        body = _frame("buffer_state", {"committed_seconds_ahead": 25}) + _frame("clip_ready", {"shot_id": "s1", "oss_url": "x"})
        respx.get(f"{BASE_URL}/api/sessions/s1/events").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        )
        received = [ev.name async for ev in client.sessions.iter_events("s1")]
        assert received == ["buffer_state", "clip_ready"]


@respx.mock
async def test_async_director_canon_edit() -> None:
    async with AsyncKinoraClient(BASE_URL, retry=FAST_RETRY, token="tok") as client:
        respx.post(f"{BASE_URL}/api/books/b1/canon_edit").mock(
            return_value=httpx.Response(200, json={"entity_key": "hero", "version": 3, "affected_shot_ids": ["s1", "s2"], "skipped_shots": 7})
        )
        r = await client.director.canon_edit("b1", entity_key="hero", changes={"name": "Jane"})
        assert r.version == 3
        assert r.affected_shot_ids == ["s1", "s2"]
        assert r.skipped_shots == 7
