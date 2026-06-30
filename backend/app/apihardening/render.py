"""Shared error-body rendering for the raw-ASGI hardening middleware.

The pure-ASGI middleware (rate limit, request limits, idempotency) emit error
responses *before* a route resolves, so they can't raise into FastAPI's exception
handlers — they must serialize a body themselves. This one helper guarantees they
all speak the **same** error surface the app is configured for:

* the legacy ``{"error": {type, message, detail?}}`` envelope (the default, so
  the desktop renderer sees no change), or
* RFC-7807 ``application/problem+json`` when ``config.problem_json_enabled``.

Keeping this in one place means a rate-limit 429 and a body-too-large 413 are
shaped identically and stay in lock-step with :mod:`.problem`.
"""

from __future__ import annotations

import json
from typing import Any

from app.apihardening.config import HardeningConfig
from app.apihardening.requestid import current_request_id


def render_error_bytes(
    *,
    code: str,
    title: str,
    status: int,
    detail: str | None,
    config: HardeningConfig,
    extensions: dict[str, Any] | None = None,
) -> tuple[bytes, str]:
    """Serialize an error to ``(body_bytes, media_type)`` in the configured shape."""
    members = {k: v for k, v in (extensions or {}).items() if v is not None}
    if config.problem_json_enabled:
        type_uri = (
            config.problem_type_base.rstrip("/") + "/" + code
            if config.problem_type_base
            else "about:blank"
        )
        body: dict[str, Any] = {
            "type": type_uri,
            "title": title,
            "status": status,
            "code": code,
        }
        if detail is not None:
            body["detail"] = detail
        if config.problem_include_request_id:
            request_id = current_request_id()
            if request_id is not None:
                body["request_id"] = request_id
        body.update(members)
        media_type = "application/problem+json"
    else:
        # Legacy envelope: keep ``type``/``message``/``detail`` exactly as
        # ``app.api.schemas.ErrorResponse`` produces them.
        body = {"error": {"type": code, "message": detail or title, "detail": members or None}}
        media_type = "application/json"
    return json.dumps(body, separators=(",", ":")).encode("utf-8"), media_type


__all__ = ["render_error_bytes"]
