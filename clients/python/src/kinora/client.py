"""The synchronous Kinora API client (``KinoraClient``).

Built on ``httpx.Client``. Exposes the API through resource namespaces that
mirror the backend route groups (auth, books, films, sessions, director, prefs,
eval, optim). Handles bearer auth, retries, timeouts, typed errors, and typed
SSE event streaming (``iter_events``).

    from kinora import KinoraClient

    with KinoraClient(base_url="http://localhost:8000") as client:
        client.auth.login("demo@kinora.local", "demo-password-123")
        for book in client.books.list():
            print(book.title, book.status)
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import TracebackType
from typing import Any, List  # noqa: UP035 - List avoids shadowing the `list` method name

import httpx

from . import errors as err
from ._transport import (
    RetryPolicy,
    TransportConfig,
    backoff_delay_s,
    build_url,
    parse_retry_after,
    should_retry_method,
)
from .events import Event, SseDecoder, parse_event
from .models import (
    BookResponse,
    BufferTracePoint,
    CanonEditResponse,
    CanonResponse,
    CommentResponse,
    ConflictChoiceResponse,
    ConflictRecordResponse,
    DirectingStyleResponse,
    EventsResponse,
    IntentResponse,
    Json,
    PageResponse,
    ResetPrefsResponse,
    SceneFilm,
    SeekResponse,
    SessionResponse,
    ShotResponse,
    TokenResponse,
    UserResponse,
)


class KinoraClient:
    """Synchronous Kinora API client."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        token: str | None = None,
        timeout_s: float = 15.0,
        retry: RetryPolicy | None = None,
        http_client: httpx.Client | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._config = TransportConfig(
            base_url=base_url,
            timeout_s=timeout_s,
            retry=retry or RetryPolicy(),
        )
        self._token = token
        self._extra_headers = dict(headers or {})
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout_s)

        self.auth = AuthResource(self)
        self.books = BooksResource(self)
        self.films = FilmsResource(self)
        self.sessions = SessionsResource(self)
        self.director = DirectorResource(self)
        self.prefs = PrefsResource(self)
        self.eval = EvalResource(self)
        self.optim = OptimResource(self)

    # -- token --------------------------------------------------------------- #

    @property
    def token(self) -> str | None:
        return self._token

    @token.setter
    def token(self, value: str | None) -> None:
        self._token = value

    def is_authenticated(self) -> bool:
        return bool(self._token)

    # -- context manager ----------------------------------------------------- #

    def __enter__(self) -> KinoraClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # -- request primitive --------------------------------------------------- #

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        out: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
            **self._extra_headers,
        }
        if self._token:
            out["Authorization"] = f"Bearer {self._token}"
        if extra:
            out.update(extra)
        return out

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Json | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> Any:
        """Issue a request, retrying per policy, and return parsed JSON (or None)."""
        url = build_url(self._config.base_url, self._config.api_prefix, path)
        label = f"{method.upper()} {path}"
        retry = self._config.retry
        attempts = retry.max_attempts if should_retry_method(method, retryable) else 1
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json,
                    params=clean_params or None,
                    files=files,
                    data=data,
                    timeout=self._config.timeout_s,
                )
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    time.sleep(backoff_delay_s(attempt, retry))
                    continue
                raise err.TimeoutError(f"request timed out: {label}", status=408, request=label) from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    time.sleep(backoff_delay_s(attempt, retry))
                    continue
                raise err.NetworkError(f"network request failed: {label}", request=label) from exc

            if response.status_code < 300:
                return _parse_body(response)
            if attempt < attempts and response.status_code in retry.retry_statuses:
                delay = parse_retry_after(response.headers.get("retry-after"))
                time.sleep(delay if delay is not None else backoff_delay_s(attempt, retry))
                continue
            raise _to_error(response, label)

        raise err.NetworkError(f"request failed: {label}", request=label)  # pragma: no cover

    def stream_lines(self, path: str, *, params: dict[str, Any] | None = None) -> Iterator[Event]:
        """Open an SSE stream and yield decoded :class:`Event` objects."""
        url = build_url(self._config.base_url, self._config.api_prefix, path)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._headers({"Accept": "text/event-stream"})
        decoder = SseDecoder()
        with self._http.stream(
            "GET", url, headers=headers, params=clean_params or None, timeout=None
        ) as response:
            if response.status_code >= 300:
                response.read()
                raise _to_error(response, f"GET {path}")
            for chunk in response.iter_text():
                for frame in decoder.feed(chunk):
                    event = parse_event(frame)
                    if event is not None:
                        yield event
        tail = decoder.flush()
        if tail is not None:
            event = parse_event(tail)
            if event is not None:
                yield event


# --------------------------------------------------------------------------- #
# Shared body / error helpers
# --------------------------------------------------------------------------- #


def _parse_body(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _to_error(response: httpx.Response, label: str) -> err.KinoraError:
    raw = response.text or None
    body: dict[str, Any] | None = None
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except (ValueError, httpx.DecodingError):
        body = None
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    return err.error_for_status(response.status_code, body, raw, label, retry_after)


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #


class AuthResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def register(self, email: str, password: str) -> UserResponse:
        """Create an account (does not log in)."""
        data = self._c.request("POST", "/auth/register", json={"email": email, "password": password})
        return UserResponse.from_dict(data)

    def login(self, email: str, password: str) -> TokenResponse:
        """Log in and store the bearer token on the client."""
        data = self._c.request("POST", "/auth/login", json={"email": email, "password": password})
        token = TokenResponse.from_dict(data)
        self._c.token = token.access_token
        return token

    def login_or_register(self, email: str, password: str) -> TokenResponse:
        """Log in, registering first if the account does not exist."""
        try:
            return self.login(email, password)
        except err.KinoraError as exc:
            if exc.status in (400, 401, 404):
                self.register(email, password)
                return self.login(email, password)
            raise

    def me(self) -> UserResponse:
        """Return the authenticated user."""
        return UserResponse.from_dict(self._c.request("GET", "/auth/me"))

    def logout(self) -> None:
        """Clear the stored token."""
        self._c.token = None


class BooksResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def upload(
        self,
        file: bytes,
        *,
        filename: str = "book.pdf",
        content_type: str = "application/pdf",
        title: str | None = None,
        author: str | None = None,
        art_direction: str | None = None,
    ) -> BookResponse:
        """Upload a PDF/EPUB and trigger Phase-A ingest (multipart)."""
        form: dict[str, Any] = {}
        if title:
            form["title"] = title
        if author:
            form["author"] = author
        if art_direction:
            form["art_direction"] = art_direction
        data = self._c.request(
            "POST",
            "/books",
            files={"file": (filename, file, content_type)},
            data=form or None,
        )
        return BookResponse.from_dict(data)

    def list(self) -> List[BookResponse]:  # noqa: UP006 - `list` method name shadows the builtin
        """List the books the current user owns (the shelf), newest first."""
        items = self._c.request("GET", "/books") or []
        return [BookResponse.from_dict(b) for b in items]

    def get(self, book_id: str) -> BookResponse:
        return BookResponse.from_dict(self._c.request("GET", f"/books/{book_id}"))

    def page(self, book_id: str, page_number: int) -> PageResponse:
        return PageResponse.from_dict(self._c.request("GET", f"/books/{book_id}/pages/{page_number}"))

    def canon(self, book_id: str) -> CanonResponse:
        return CanonResponse.from_dict(self._c.request("GET", f"/books/{book_id}/canon"))

    def shots(self, book_id: str) -> List[ShotResponse]:  # noqa: UP006 - `list` method shadows builtin
        items = self._c.request("GET", f"/books/{book_id}/shots") or []
        return [ShotResponse.from_dict(s) for s in items]

    def wait_until_ready(
        self,
        book_id: str,
        *,
        interval_s: float = 2.0,
        timeout_s: float = 600.0,
    ) -> BookResponse:
        """Poll until the book reaches ``status == 'ready'`` (or raise on timeout/failure)."""
        deadline = time.monotonic() + timeout_s
        while True:
            book = self.get(book_id)
            if book.status == "ready":
                return book
            if book.status == "failed":
                raise err.KinoraError(f"book {book_id} ingest failed", type="ingest_failed")
            if time.monotonic() >= deadline:
                raise err.TimeoutError(f"book {book_id} not ready after timeout", type="timeout")
            time.sleep(interval_s)


class FilmsResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def events(self, book_id: str) -> EventsResponse:
        return EventsResponse.from_dict(self._c.request("GET", f"/books/{book_id}/events"))

    def scene_film(self, book_id: str, scene_id: str) -> SceneFilm:
        return SceneFilm.from_dict(self._c.request("GET", f"/books/{book_id}/scenes/{scene_id}/film"))


class SessionsResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def create(self, book_id: str, *, focus_word: int = 0, mode: str = "viewer") -> SessionResponse:
        data = self._c.request(
            "POST", "/sessions", json={"book_id": book_id, "focus_word": focus_word, "mode": mode}
        )
        return SessionResponse.from_dict(data)

    def get(self, session_id: str) -> SessionResponse:
        return SessionResponse.from_dict(self._c.request("GET", f"/sessions/{session_id}"))

    def intent(
        self, session_id: str, *, focus_word: int, velocity: float = 4.0, mode: str | None = None
    ) -> IntentResponse:
        body: Json = {"focus_word": focus_word, "velocity": velocity}
        if mode is not None:
            body["mode"] = mode
        data = self._c.request("POST", f"/sessions/{session_id}/intent", json=body, retryable=True)
        return IntentResponse.from_dict(data)

    def seek(self, session_id: str, word: int) -> SeekResponse:
        data = self._c.request("POST", f"/sessions/{session_id}/seek", json={"word": word}, retryable=True)
        return SeekResponse.from_dict(data)

    def iter_events(self, session_id: str, *, token_in_query: bool = False) -> Iterator[Event]:
        """Stream this session's generation events as typed :class:`Event` objects."""
        params = {"token": self._c.token} if token_in_query and self._c.token else None
        yield from self._c.stream_lines(f"/sessions/{session_id}/events", params=params)


class DirectorResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def comment(
        self, session_id: str, *, shot_id: str, note: str, region_png: str | None = None
    ) -> CommentResponse:
        body: Json = {"shot_id": shot_id, "note": note}
        if region_png is not None:
            body["region_png"] = region_png
        data = self._c.request("POST", f"/sessions/{session_id}/comment", json=body)
        return CommentResponse.from_dict(data)

    def canon_edit(
        self, book_id: str, *, entity_key: str, changes: Json, valid_from_beat: int | None = None
    ) -> CanonEditResponse:
        body: Json = {"entity_key": entity_key, "changes": changes}
        if valid_from_beat is not None:
            body["valid_from_beat"] = valid_from_beat
        return CanonEditResponse.from_dict(self._c.request("POST", f"/books/{book_id}/canon_edit", json=body))

    def conflict_choice(
        self, session_id: str, *, conflict_id: str, option: str
    ) -> ConflictChoiceResponse:
        body = {"conflict_id": conflict_id, "option": option}
        return ConflictChoiceResponse.from_dict(
            self._c.request("POST", f"/sessions/{session_id}/conflict_choice", json=body)
        )

    def conflicts(self, session_id: str) -> list[ConflictRecordResponse]:
        items = self._c.request("GET", f"/sessions/{session_id}/conflicts") or []
        return [ConflictRecordResponse.from_dict(c) for c in items]

    def demo_conflict(self, session_id: str) -> ConflictRecordResponse:
        return ConflictRecordResponse.from_dict(
            self._c.request("POST", f"/sessions/{session_id}/demo/conflict")
        )


class PrefsResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def me(self) -> DirectingStyleResponse:
        return DirectingStyleResponse.from_dict(self._c.request("GET", "/me/prefs"))

    def book(self, book_id: str) -> DirectingStyleResponse:
        return DirectingStyleResponse.from_dict(self._c.request("GET", f"/books/{book_id}/prefs"))

    def reset_me(self) -> ResetPrefsResponse:
        return ResetPrefsResponse.from_dict(self._c.request("DELETE", "/me/prefs"))

    def reset_book(self, book_id: str) -> ResetPrefsResponse:
        return ResetPrefsResponse.from_dict(self._c.request("DELETE", f"/books/{book_id}/prefs"))


class EvalResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def buffer_trace(
        self, session_id: str, *, velocity: float | None = None, duration_s: float | None = None
    ) -> list[BufferTracePoint]:
        params = {"velocity": velocity, "duration_s": duration_s}
        items = self._c.request("GET", f"/eval/buffer-trace/{session_id}", params=params) or []
        return [BufferTracePoint.from_dict(p) for p in items]

    def report(self, book_id: str) -> Json:
        result: Json = self._c.request("GET", f"/eval/report/{book_id}")
        return result


class OptimResource:
    def __init__(self, client: KinoraClient) -> None:
        self._c = client

    def cost(self) -> Json:
        result: Json = self._c.request("GET", "/optim/cost")
        return result

    def perf(self) -> Json:
        result: Json = self._c.request("GET", "/optim/perf")
        return result


__all__ = ["KinoraClient"]
