"""The single network seam for every connector.

Connectors **never** import ``httpx`` or open a socket directly. They are handed
an :class:`AsyncHttpClient` and call it. In production the container wires the
real :class:`HttpxClient`; in tests the suite injects :class:`FakeHttpClient`
with canned responses. This is what keeps "make NO real third-party/network
calls" true: there is exactly one place that can touch the network, and tests
replace it.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.integrations.errors import (
    AuthExpired,
    PermanentError,
    RateLimited,
    TransientError,
)


@dataclass(frozen=True)
class HttpResponse:
    """A minimal, framework-agnostic HTTP response."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    #: The request URL (echoed back so callers can resolve relative redirects).
    url: str = ""

    @property
    def text(self) -> str:
        """The body decoded as UTF-8 (lenient)."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON (raises on malformed JSON)."""
        return _json.loads(self.text or "null")

    @property
    def ok(self) -> bool:
        """True for 2xx."""
        return 200 <= self.status < 300

    def raise_for_status(self) -> HttpResponse:
        """Map common HTTP failure codes onto the integrations error hierarchy.

        429 / 5xx → :class:`TransientError` family (retryable); 401/403 →
        :class:`AuthExpired`; other 4xx → :class:`PermanentError`. 2xx/3xx pass.
        """
        if self.ok or 300 <= self.status < 400:
            return self
        if self.status == 429:
            retry = self.headers.get("retry-after") or self.headers.get("Retry-After")
            raise RateLimited(
                f"rate limited ({self.status})",
                retry_after_s=_parse_retry_after(retry),
            )
        if self.status in (401, 403):
            raise AuthExpired(f"authorization failed ({self.status})")
        if 500 <= self.status < 600:
            raise TransientError(f"upstream error ({self.status})")
        raise PermanentError(f"request failed ({self.status}): {self.text[:200]}")


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a numeric ``Retry-After`` header into seconds (ignore HTTP-dates)."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


@runtime_checkable
class AsyncHttpClient(Protocol):
    """The async HTTP seam connectors are handed."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        timeout_s: float = 30.0,
    ) -> HttpResponse: ...


class HttpxClient:
    """The production :class:`AsyncHttpClient`, backed by a lazily-built httpx client.

    ``httpx`` is imported lazily inside ``request`` so importing the integrations
    package (and constructing the container) never pulls the network stack in.
    """

    def __init__(self, *, user_agent: str = "Kinora/1.0 (+integrations)") -> None:
        self._user_agent = user_agent
        self._client: Any | None = None

    def _ensure(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        timeout_s: float = 30.0,
    ) -> HttpResponse:
        import httpx

        client = self._ensure()
        try:
            resp = await client.request(
                method.upper(),
                url,
                params=params,
                headers=headers,
                json=json,
                data=data,
                timeout=timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"request timed out: {url}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"network error: {exc}") from exc
        return HttpResponse(
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            content=resp.content,
            url=str(resp.url),
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client if one was built."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class RecordedRequest:
    """One request the :class:`FakeHttpClient` observed (for assertions)."""

    method: str
    url: str
    params: dict[str, Any] | None
    headers: dict[str, str] | None
    json: Any | None
    data: Any | None


Handler = Any  # callable(RecordedRequest) -> HttpResponse | response sequence


class FakeHttpClient:
    """A scriptable in-memory :class:`AsyncHttpClient` for tests.

    Register responses by ``(METHOD, url-substring)`` route. A route value may be
    a single :class:`HttpResponse`, a list (consumed one per call — handy for
    pagination), or a callable taking the :class:`RecordedRequest`. Unmatched
    requests raise so a test never silently hits a real network.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, str, Any]] = []
        self.requests: list[RecordedRequest] = []

    def add(self, method: str, url_contains: str, response: Any) -> FakeHttpClient:
        """Register a response for requests whose URL contains ``url_contains``."""
        self._routes.append((method.upper(), url_contains, response))
        return self

    def json_response(
        self, method: str, url_contains: str, payload: Any, *, status: int = 200
    ) -> FakeHttpClient:
        """Convenience: register a JSON body."""
        body = _json.dumps(payload).encode("utf-8")
        return self.add(
            method,
            url_contains,
            HttpResponse(status=status, headers={"content-type": "application/json"}, content=body),
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        timeout_s: float = 30.0,
    ) -> HttpResponse:
        rec = RecordedRequest(
            method=method.upper(), url=url, params=params, headers=headers, json=json, data=data
        )
        self.requests.append(rec)
        for i, (m, frag, value) in enumerate(self._routes):
            if m == rec.method and frag in url:
                if isinstance(value, list):
                    if not value:
                        raise AssertionError(f"FakeHttpClient route exhausted: {m} {frag}")
                    nxt = value.pop(0)
                    return nxt(rec) if callable(nxt) else nxt
                if callable(value):
                    result = value(rec)
                    return result
                # Single response: keep it (idempotent re-use) unless it's a list.
                self._routes[i] = (m, frag, value)
                return value
        raise AssertionError(f"FakeHttpClient: no route for {rec.method} {url}")


__all__ = [
    "AsyncHttpClient",
    "FakeHttpClient",
    "HttpResponse",
    "HttpxClient",
    "RecordedRequest",
]
