"""Transport: URL building, retries, backoff, error mapping. All HTTP mocked."""

from __future__ import annotations

import httpx
import pytest
import respx

from kinora import (
    AuthError,
    BudgetExceededError,
    ConflictError,
    ForbiddenError,
    KinoraClient,
    KinoraError,
    LiveVideoDisabledError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitError,
    ServerError,
    UploadError,
    ValidationError,
)
from kinora._transport import (
    RetryPolicy,
    backoff_delay_s,
    build_url,
    parse_retry_after,
    should_retry_method,
)

from conftest import BASE_URL, FAST_RETRY


def test_build_url_prefixes_and_no_double_prefix() -> None:
    assert build_url("http://h:8000", "/api", "/books") == "http://h:8000/api/books"
    assert build_url("http://h:8000/", "/api", "books") == "http://h:8000/api/books"
    assert build_url("http://h:8000", "/api", "/api/books") == "http://h:8000/api/books"


def test_should_retry_method() -> None:
    assert should_retry_method("GET", None) is True
    assert should_retry_method("POST", None) is False
    assert should_retry_method("POST", True) is True
    assert should_retry_method("GET", False) is False


def test_backoff_is_bounded() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=4.0)
    for attempt in range(1, 6):
        assert 0.0 <= backoff_delay_s(attempt, policy) <= 4.0


def test_parse_retry_after() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-date") is None


@respx.mock
def test_attaches_bearer_token(client: KinoraClient) -> None:
    client.token = "tok-123"
    route = respx.get(f"{BASE_URL}/api/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "a@b.co"})
    )
    client.auth.me()
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-123"


@respx.mock
def test_json_body_and_parsing(client: KinoraClient) -> None:
    respx.post(f"{BASE_URL}/api/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "token_type": "bearer", "expires_in": 3600})
    )
    tok = client.auth.login("a@b.co", "password1")
    assert tok.access_token == "abc"
    assert client.is_authenticated()


@respx.mock
def test_204_returns_none(client: KinoraClient) -> None:
    respx.delete(f"{BASE_URL}/api/me/prefs").mock(return_value=httpx.Response(200, json={"scope": "user", "cleared": 0}))
    result = client.prefs.reset_me()
    assert result.cleared == 0


ERROR_CASES = [
    (401, "invalid_credentials", AuthError),
    (403, "forbidden", ForbiddenError),
    (404, "book_not_found", NotFoundError),
    (409, "email_taken", ConflictError),
    (409, "live_video_disabled", LiveVideoDisabledError),
    (402, "budget_exceeded", BudgetExceededError),
    (413, "file_too_large", UploadError),
    (415, "unsupported_media_type", UploadError),
    (422, "validation_error", ValidationError),
    (502, "provider_error", ProviderError),
    (500, "internal_error", ServerError),
]


@pytest.mark.parametrize(("status", "type_", "cls"), ERROR_CASES)
@respx.mock
def test_error_mapping(client: KinoraClient, status: int, type_: str, cls: type[KinoraError]) -> None:
    respx.get(f"{BASE_URL}/api/books/x").mock(
        return_value=httpx.Response(status, json={"error": {"type": type_, "message": "boom", "detail": {"k": 1}}})
    )
    with pytest.raises(cls) as exc_info:
        client.books.get("x")
    err = exc_info.value
    assert err.status == status
    assert err.type == type_
    assert err.detail == {"k": 1}
    assert err.request == "GET /books/x"


@respx.mock
def test_429_maps_to_rate_limit_with_retry_after(client: KinoraClient) -> None:
    respx.get(f"{BASE_URL}/api/books/x").mock(
        return_value=httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"type": "rate", "message": "slow"}})
    )
    with pytest.raises(RateLimitError) as exc_info:
        client.books.get("x")
    assert exc_info.value.retry_after_s == 2.0


@respx.mock
def test_retry_then_success_on_get(client: KinoraClient) -> None:
    route = respx.get(f"{BASE_URL}/api/books").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"type": "x", "message": "down"}}),
            httpx.Response(503, json={"error": {"type": "x", "message": "down"}}),
            httpx.Response(200, json=[{"id": "b1", "title": "A", "status": "ready"}]),
        ]
    )
    books = client.books.list()
    assert len(books) == 1
    assert route.call_count == 3


@respx.mock
def test_post_not_retried_by_default(client: KinoraClient) -> None:
    route = respx.post(f"{BASE_URL}/api/auth/register").mock(
        return_value=httpx.Response(503, json={"error": {"type": "x", "message": "down"}})
    )
    with pytest.raises(ServerError):
        client.auth.register("a@b.co", "password1")
    assert route.call_count == 1


@respx.mock
def test_intent_is_retryable_post(client: KinoraClient) -> None:
    client.token = "tok"
    route = respx.post(f"{BASE_URL}/api/sessions/s1/intent").mock(
        side_effect=[
            httpx.Response(502, json={"error": {"type": "provider_error", "message": "x"}}),
            httpx.Response(200, json={"session_id": "s1", "settled": True, "committed_seconds_ahead": 30}),
        ]
    )
    r = client.sessions.intent("s1", focus_word=10, velocity=4.0)
    assert r.committed_seconds_ahead == 30
    assert route.call_count == 2


@respx.mock
def test_network_error_after_exhaustion(client: KinoraClient) -> None:
    respx.get(f"{BASE_URL}/api/books").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NetworkError):
        client.books.list()


@respx.mock
def test_max_attempts_for_retryable_status(client: KinoraClient) -> None:
    route = respx.get(f"{BASE_URL}/api/books").mock(
        return_value=httpx.Response(503, json={"error": {"type": "x", "message": "down"}})
    )
    with pytest.raises(ServerError):
        client.books.list()
    assert route.call_count == FAST_RETRY.max_attempts
