"""Sync client resource methods + model parsing. All HTTP mocked via respx."""

from __future__ import annotations

import httpx
import respx

from kinora import KinoraClient
from kinora.models import BookResponse, CanonResponse, CommentResponse

from conftest import BASE_URL


@respx.mock
def test_login_or_register_registers_after_401(client: KinoraClient) -> None:
    respx.post(f"{BASE_URL}/api/auth/login").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"type": "invalid_credentials", "message": "no"}}),
            httpx.Response(200, json={"access_token": "abc", "token_type": "bearer", "expires_in": 3600}),
        ]
    )
    register = respx.post(f"{BASE_URL}/api/auth/register").mock(
        return_value=httpx.Response(201, json={"id": "u1", "email": "a@b.co"})
    )
    tok = client.auth.login_or_register("a@b.co", "password1")
    assert tok.access_token == "abc"
    assert register.called
    assert client.token == "abc"


@respx.mock
def test_books_list_parses_models(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/books").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "b1", "title": "A", "status": "ready", "progress": 1.0},
                {"id": "b2", "title": "B", "status": "importing"},
            ],
        )
    )
    books = client.books.list()
    assert [b.id for b in books] == ["b1", "b2"]
    assert isinstance(books[0], BookResponse)
    assert books[0].progress == 1.0


@respx.mock
def test_unknown_fields_preserved_in_extra(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/books/b1").mock(
        return_value=httpx.Response(200, json={"id": "b1", "title": "A", "status": "ready", "future_field": 42})
    )
    book = client.books.get("b1")
    assert book.extra["future_field"] == 42
    assert book.get("future_field") == 42
    assert book.get("title") == "A"


@respx.mock
def test_canon_nested_models(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/books/b1/canon").mock(
        return_value=httpx.Response(
            200,
            json={
                "book_id": "b1",
                "entities": [{"id": "hero", "type": "character", "name": "Jane", "version": 2}],
                "states": [
                    {
                        "id": "st1",
                        "subject_entity_key": "hero",
                        "predicate": "has",
                        "object_value": "sword",
                        "valid_from_beat": 0,
                        "version": 1,
                        "active": True,
                    }
                ],
                "markdown": "# Canon",
            },
        )
    )
    canon = client.books.canon("b1")
    assert isinstance(canon, CanonResponse)
    assert canon.entities[0].name == "Jane"
    assert canon.entities[0].version == 2
    assert canon.states[0].object_value == "sword"


@respx.mock
def test_upload_multipart(client: KinoraClient) -> None:
    client.token = "tok"
    route = respx.post(f"{BASE_URL}/api/books").mock(
        return_value=httpx.Response(201, json={"id": "b9", "title": "My Book", "status": "importing"})
    )
    book = client.books.upload(b"%PDF-1.7 fake", filename="my.pdf", title="My Book")
    assert book.id == "b9"
    req = route.calls.last.request
    assert b"multipart/form-data" in req.headers["content-type"].encode()
    assert b"My Book" in req.content


@respx.mock
def test_wait_until_ready_polls(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/books/b1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "b1", "title": "A", "status": "importing", "progress": 0.5}),
            httpx.Response(200, json={"id": "b1", "title": "A", "status": "ready", "progress": 1.0}),
        ]
    )
    book = client.books.wait_until_ready("b1", interval_s=0.0)
    assert book.status == "ready"


@respx.mock
def test_session_create_and_intent(client: KinoraClient) -> None:
    client.token = "tok"
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(
            201,
            json={"session_id": "s1", "book_id": "b1", "focus_word": 0, "velocity_wps": 4.0, "mode": "viewer", "committed_seconds_ahead": 0.0},
        )
    )
    session = client.sessions.create("b1")
    assert session.session_id == "s1"

    intent_route = respx.post(f"{BASE_URL}/api/sessions/s1/intent").mock(
        return_value=httpx.Response(200, json={"session_id": "s1", "settled": True, "committed_seconds_ahead": 30.0, "promoted": ["shotA"]})
    )
    r = client.sessions.intent("s1", focus_word=120, velocity=5.0)
    assert r.promoted == ["shotA"]
    # mode omitted -> not in body
    body = intent_route.calls.last.request.content
    assert b"mode" not in body


@respx.mock
def test_director_comment_learned_priors(client: KinoraClient) -> None:
    client.token = "tok"
    respx.post(f"{BASE_URL}/api/sessions/s1/comment").mock(
        return_value=httpx.Response(
            200,
            json={
                "shot_id": "shot1",
                "agent": "cinematographer",
                "aspect": "pacing",
                "message": "Noted",
                "job_id": "job9",
                "learned": [{"kind": "pacing", "bias": -0.3, "weight": 1.0, "label": "Slower shots", "detail": "", "applied": True}],
            },
        )
    )
    r = client.director.comment("s1", shot_id="shot1", note="slower please")
    assert isinstance(r, CommentResponse)
    assert r.learned[0].kind == "pacing"
    assert r.learned[0].applied is True


@respx.mock
def test_director_conflict_choice(client: KinoraClient) -> None:
    client.token = "tok"
    respx.post(f"{BASE_URL}/api/sessions/s1/conflict_choice").mock(
        return_value=httpx.Response(200, json={"conflict_id": "cf_x", "option": "honor_canon", "status": "applied", "shot_id": "x"})
    )
    r = client.director.conflict_choice("s1", conflict_id="cf_x", option="honor_canon")
    assert r.status == "applied"


@respx.mock
def test_prefs_and_eval_and_optim(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/me/prefs").mock(
        return_value=httpx.Response(200, json={"scope": "user", "priors": [{"kind": "pacing", "bias": 0.1, "weight": 1.0, "label": "x", "detail": "y", "applied": False}]})
    )
    style = client.prefs.me()
    assert style.scope == "user"
    assert style.priors[0].kind == "pacing"

    respx.get(f"{BASE_URL}/api/eval/buffer-trace/s1").mock(
        return_value=httpx.Response(200, json=[{"t": 0.0, "committed_seconds_ahead": 25.0, "low": 25.0, "high": 75.0}])
    )
    pts = client.eval.buffer_trace("s1", velocity=5.0, duration_s=120.0)
    assert pts[0].high == 75.0

    respx.get(f"{BASE_URL}/api/optim/cost").mock(return_value=httpx.Response(200, json={"rollup": {"total": {}}}))
    cost = client.optim.cost()
    assert "rollup" in cost


@respx.mock
def test_films_events_and_scene(client: KinoraClient) -> None:
    client.token = "tok"
    respx.get(f"{BASE_URL}/api/books/b1/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "book_id": "b1",
                "url_ttl_s": 3600,
                "events": [{"event_id": "sc1", "event_index": 0, "book_id": "b1", "page_start": 1, "page_end": 2, "word_range": [0, 100], "stitched": True, "shot_count": 3, "sync_map": {"scene_id": "sc1", "duration_s": 12.0, "segments": []}}],
                "restore": {"session_id": "s1", "focus_word": 50, "mode": "viewer"},
            },
        )
    )
    events = client.films.events("b1")
    assert events.events[0].event_id == "sc1"
    assert events.restore is not None
    assert events.restore.focus_word == 50
