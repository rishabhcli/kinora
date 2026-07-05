"""CSRF protection middleware for cookie-authenticated requests (kinora.md §12).

Kinora's primary clients are the Electron desktop app and headless callers, both
of which authenticate with an ``Authorization: Bearer`` token or an ``X-API-Key``
header — neither is vulnerable to CSRF, because a browser will not attach those
headers cross-site. The risk surface is **cookie-borne** auth (a browser
deployment that stores the session in a cookie); for those requests this middleware
enforces the **double-submit cookie** pattern:

* a non-secret random token is set in a readable cookie (``csrf_cookie_name``),
* unsafe requests (POST/PUT/PATCH/DELETE) that authenticate **via a cookie** must
  echo that token in the ``csrf_header_name`` header, and the two must match.

Requests that carry an ``Authorization`` / ``X-API-Key`` header are exempt (they
are not cookie-auth and therefore not CSRF-exposed), as are safe methods and the
docs/health/metrics endpoints. The check is constant-time. Implemented as pure
ASGI so it never buffers bodies or interferes with SSE/WebSocket streaming.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.security import constant_time_compare, generate_token

#: Methods that mutate state and therefore require a CSRF token under cookie auth.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Paths exempt from CSRF (no cookie-auth state change happens here).
_EXEMPT_PREFIXES = ("/health", "/ready", "/metrics", "/docs", "/openapi", "/redoc")


class CsrfMiddleware:
    """Enforce the double-submit-cookie CSRF check for cookie-authenticated writes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool = True,
        cookie_name: str = "kinora_csrf",
        header_name: str = "X-CSRF-Token",
        secure_cookie: bool = False,
    ) -> None:
        self.app = app
        self._enabled = enabled
        self._cookie = cookie_name
        self._header = header_name.lower()
        self._secure = secure_cookie

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method: str = scope.get("method", "GET")
        path: str = scope.get("path", "/")

        needs_check = (
            method in _UNSAFE_METHODS
            and not path.startswith(_EXEMPT_PREFIXES)
            # Bearer / API-key callers are not CSRF-exposed (browsers don't attach
            # those cross-site). Only cookie-auth requests are checked.
            and "authorization" not in headers
            and "x-api-key" not in headers
            and self._has_session_cookie(headers.get("cookie", ""))
        )
        if needs_check and not self._token_matches(headers):
            await _forbidden(send)
            return

        cookie_token = self._read_cookie(headers.get("cookie", ""), self._cookie)
        issue_token = cookie_token or generate_token(24)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and cookie_token is None:
                raw = list(message.get("headers", []))
                raw.append((b"set-cookie", self._cookie_header(issue_token).encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _token_matches(self, headers: dict[str, str]) -> bool:
        sent = headers.get(self._header, "")
        cookie_token = self._read_cookie(headers.get("cookie", ""), self._cookie)
        if not sent or not cookie_token:
            return False
        return constant_time_compare(sent, cookie_token)

    @staticmethod
    def _has_session_cookie(cookie_header: str) -> bool:
        # Treat any cookie whose name hints at a session as cookie-auth.
        lowered = cookie_header.lower()
        return any(name in lowered for name in ("session", "token", "kinora_auth"))

    @staticmethod
    def _read_cookie(cookie_header: str, name: str) -> str | None:
        for part in cookie_header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _cookie_header(self, token: str) -> str:
        attrs = [f"{self._cookie}={token}", "Path=/", "SameSite=Lax"]
        if self._secure:
            attrs.append("Secure")
        # Readable by JS so the SPA can echo it back (double-submit requires this).
        return "; ".join(attrs)


async def _forbidden(send: Send) -> None:
    body = b'{"error":{"type":"csrf_failed","message":"missing or invalid CSRF token"}}'
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["CsrfMiddleware"]
