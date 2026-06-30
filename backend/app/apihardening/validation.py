"""Request validation / limits middleware (body size + content-type).

:class:`RequestLimitsMiddleware` enforces two coarse, cheap, defence-in-depth
limits before a route runs:

* **Body size** — rejects a request whose declared ``Content-Length`` exceeds the
  cap with a ``413`` *before reading the body*, and (for chunked / unknown-length
  uploads) wraps the ASGI ``receive`` so a body that *grows* past the cap mid-
  stream is cut off with the same ``413``. This protects every route, including
  the PDF-upload path, from an unbounded-memory request.
* **Content-Type** — on body methods, rejects a request whose media type is not
  in the allow-list with a ``415``, so a route expecting JSON never has to defend
  against ``text/html`` or a missing type. Configurable exempt prefixes let raw
  upload routes accept ``application/pdf`` / octet-stream.

It is raw ASGI (no body buffering of its own) and renders the rejection through
:func:`app.apihardening.render.render_error_bytes`, so the body matches the app's
configured error surface (legacy envelope or problem+json).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.apihardening.config import HardeningConfig
from app.apihardening.render import render_error_bytes


class _BodyTooLarge(Exception):  # noqa: N818 - internal control-flow signal, not a public error
    """Internal signal: the streamed body grew past the cap."""


class RequestLimitsMiddleware:
    """Cap request body size and enforce a content-type allow-list."""

    def __init__(self, app: ASGIApp, *, config: HardeningConfig | None = None) -> None:
        self.app = app
        self._config = config or HardeningConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cfg = self._config
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        body_capped = cfg.max_body_bytes > 0 and not any(
            path.startswith(p) for p in cfg.body_size_exempt_prefixes
        )

        # --- declared-length fast path ---
        if body_capped:
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = -1
                if declared > cfg.max_body_bytes:
                    await self._reject(
                        send,
                        code="payload_too_large",
                        title="Payload Too Large",
                        status=413,
                        detail=f"request body exceeds the {cfg.max_body_bytes}-byte limit",
                        extensions={"limit_bytes": cfg.max_body_bytes},
                    )
                    return

        # --- content-type allow-list ---
        if (
            method in cfg.body_methods
            and cfg.allowed_content_types
            and not any(path.startswith(p) for p in cfg.content_type_exempt_prefixes)
        ):
            ctype_raw = headers.get(b"content-type")
            ctype = ""
            if ctype_raw:
                ctype = ctype_raw.decode("latin-1").split(";", 1)[0].strip().lower()
            if ctype not in cfg.allowed_content_types:
                await self._reject(
                    send,
                    code="unsupported_media_type",
                    title="Unsupported Media Type",
                    status=415,
                    detail=f"content-type {ctype or '(missing)'!r} is not accepted here",
                    extensions={"allowed": sorted(cfg.allowed_content_types)},
                )
                return

        # --- streamed size guard (chunked / unknown length) ---
        if not body_capped:
            await self.app(scope, receive, send)
            return

        seen = 0
        limit = cfg.max_body_bytes

        async def guarded_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, guarded_receive, send)
        except _BodyTooLarge:
            await self._reject(
                send,
                code="payload_too_large",
                title="Payload Too Large",
                status=413,
                detail=f"request body exceeds the {limit}-byte limit",
                extensions={"limit_bytes": limit},
            )

    async def _reject(
        self,
        send: Send,
        *,
        code: str,
        title: str,
        status: int,
        detail: str,
        extensions: dict[str, object] | None = None,
    ) -> None:
        body, media_type = render_error_bytes(
            code=code,
            title=title,
            status=status,
            detail=detail,
            config=self._config,
            extensions=extensions,
        )
        headers = [
            (b"content-type", media_type.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = ["RequestLimitsMiddleware"]
