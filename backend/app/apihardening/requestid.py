"""Request-id / correlation-id propagation (pure ASGI, no body buffering).

:class:`RequestIdMiddleware` assigns every request a stable id (minted, or — when
configured to trust the edge — echoed from an inbound ``X-Request-ID``), exposes
it on ``request.state.request_id`` and via the :func:`current_request_id`
contextvar, binds it onto the structlog contextvars so every log line in the
request is correlated, and stamps both the request-id and the (separate)
correlation-id header onto the response.

It is implemented as raw ASGI so it never buffers the response body and is
transparent to streaming SSE / WebSocket transports (which it passes straight
through).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.apihardening.config import HardeningConfig

#: The canonical header name (the default config value, exported for callers).
REQUEST_ID_HEADER = "X-Request-ID"

#: Per-request id, readable anywhere downstream (handlers, services, error bodies)
#: without threading it through call sites.
_request_id: ContextVar[str | None] = ContextVar("kinora_request_id", default=None)


def current_request_id() -> str | None:
    """Return the current request's id, or ``None`` outside a request."""
    return _request_id.get()


def _new_id() -> str:
    return uuid.uuid4().hex


def _is_safe_id(value: str, *, max_len: int = 128) -> bool:
    """Reject inbound ids that aren't a sane token (header-injection defence)."""
    if not value or len(value) > max_len:
        return False
    return all(33 <= ord(ch) <= 126 for ch in value)


class RequestIdMiddleware:
    """Mint/echo a request-id, bind it for logging, and stamp it on the response."""

    def __init__(self, app: ASGIApp, *, config: HardeningConfig | None = None) -> None:
        self.app = app
        self._config = config or HardeningConfig()
        self._req_header = self._config.request_id_header
        self._req_header_lower = self._req_header.lower().encode("latin-1")
        self._corr_header = self._config.correlation_id_header
        self._corr_header_lower = self._corr_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        request_id = self._resolve_id(headers, self._req_header_lower)
        # The correlation id traverses a whole client interaction; default it to
        # the request id when the client does not supply its own.
        correlation_id = (
            self._resolve_id(headers, self._corr_header_lower, mint=False) or request_id
        )

        token = _request_id.set(request_id)
        structlog.contextvars.bind_contextvars(
            request_id=request_id, correlation_id=correlation_id
        )

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                if self._req_header_lower not in present:
                    raw.append((self._req_header_lower, request_id.encode("latin-1")))
                if self._corr_header_lower not in present:
                    raw.append((self._corr_header_lower, correlation_id.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        # Expose on request.state for handlers that prefer it over the contextvar.
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            state["request_id"] = request_id
            state["correlation_id"] = correlation_id

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "correlation_id")
            _request_id.reset(token)

    def _resolve_id(
        self, headers: dict[bytes, bytes], header_lower: bytes, *, mint: bool = True
    ) -> str:
        if self._config.trust_inbound_request_id:
            raw = headers.get(header_lower)
            if raw is not None:
                candidate = raw.decode("latin-1").strip()
                if _is_safe_id(candidate):
                    return candidate
        return _new_id() if mint else ""


__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "current_request_id"]
