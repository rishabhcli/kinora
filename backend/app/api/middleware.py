"""Security-hardening middleware for the gateway (kinora.md §12).

:class:`SecurityHeadersMiddleware` stamps a conservative set of security headers
onto every response (including error envelopes), implemented as lightweight pure
ASGI so it adds headers without buffering bodies or interfering with streaming
SSE/WebSocket transports.

The Content-Security-Policy is permissive enough for the OpenAPI docs UI
(Swagger loads from the jsDelivr CDN and uses inline styles/scripts) while
locking everything else to ``'self'``. HSTS is emitted **only** outside the
local environment so developers hitting plain ``http://localhost`` are never
pinned to HTTPS.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: A sane default CSP for a JSON API that also serves Swagger UI at ``/docs``.
DEFAULT_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "object-src 'none'"
)

#: HSTS: two years, include subdomains, preload-eligible (prod only).
DEFAULT_HSTS = "max-age=63072000; includeSubDomains; preload"


class SecurityHeadersMiddleware:
    """Add ``nosniff``/``X-Frame-Options``/``Referrer-Policy``/CSP (+HSTS) headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts: bool = False,
        csp: str = DEFAULT_CSP,
        referrer_policy: str = "no-referrer",
        hsts_value: str = DEFAULT_HSTS,
    ) -> None:
        self.app = app
        self._hsts = hsts
        # Header name/value pairs encoded once (ASGI carries raw bytes).
        headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", referrer_policy.encode("latin-1")),
            (b"content-security-policy", csp.encode("latin-1")),
        ]
        if hsts:
            headers.append((b"strict-transport-security", hsts_value.encode("latin-1")))
        self._headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                for name, value in self._headers:
                    if name not in present:
                        raw.append((name, value))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)


__all__ = ["DEFAULT_CSP", "DEFAULT_HSTS", "SecurityHeadersMiddleware"]
