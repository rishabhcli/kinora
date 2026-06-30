"""Provider-specific payload parsing → the canonical :class:`ProviderCallback`.

Each async media provider ships a different JSON body. A parser's only job is to
pull the four things the platform actually needs — *which task*, *what status*,
*the asset URL*, *any error* — out of one provider's shape and return a validated
:class:`ProviderCallback`. Everything provider-idiosyncratic is collapsed here so
the gateway and the sink stay provider-agnostic.

Design choices that keep this robust against real-world callback chaos:

* **Tolerant status mapping.** Unknown status strings map to
  :attr:`CallbackStatus.UNKNOWN` (kept in ``raw_status``) rather than raising —
  providers add states over time and a gateway must not 500 on a new word.
* **Derived idempotency key.** When a provider sends a per-delivery id we use it;
  otherwise we derive ``"{task_id}:{status}"`` so the *same* transition dedups
  but distinct transitions (running → succeeded) each get processed once.
* **A built-in canonical parser.** Our own internal services (and tests) can post
  the already-canonical shape; the registry falls back to it for any provider
  without a bespoke parser, so a newly-added provider is callable day one.

A parser raises :class:`MalformedPayloadError` only when the body is *unusable*
(not an object, or missing the task id) — i.e. a 422, never a crash.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.video.webhooks.errors import MalformedPayloadError
from app.video.webhooks.models import CallbackStatus, ProviderCallback

#: A parser maps a decoded JSON object → a canonical callback.
Parser = Callable[[str, dict[str, Any]], ProviderCallback]


# Maps every provider's status vocabulary onto the canonical lattice. Lower-cased
# at lookup time; anything absent falls through to UNKNOWN (tolerated).
_STATUS_ALIASES: dict[str, CallbackStatus] = {
    # succeeded
    "succeeded": CallbackStatus.SUCCEEDED,
    "success": CallbackStatus.SUCCEEDED,
    "successful": CallbackStatus.SUCCEEDED,
    "done": CallbackStatus.SUCCEEDED,
    "complete": CallbackStatus.SUCCEEDED,
    "completed": CallbackStatus.SUCCEEDED,
    "finished": CallbackStatus.SUCCEEDED,
    "ready": CallbackStatus.SUCCEEDED,
    # failed
    "failed": CallbackStatus.FAILED,
    "failure": CallbackStatus.FAILED,
    "error": CallbackStatus.FAILED,
    "errored": CallbackStatus.FAILED,
    "rejected": CallbackStatus.FAILED,
    # cancelled
    "cancelled": CallbackStatus.CANCELLED,
    "canceled": CallbackStatus.CANCELLED,
    "aborted": CallbackStatus.CANCELLED,
    # running / non-terminal
    "running": CallbackStatus.RUNNING,
    "processing": CallbackStatus.RUNNING,
    "in_progress": CallbackStatus.RUNNING,
    "pending": CallbackStatus.RUNNING,
    "queued": CallbackStatus.RUNNING,
    "submitted": CallbackStatus.RUNNING,
}


def map_status(raw: str | None) -> CallbackStatus:
    """Collapse a provider status string onto the canonical lattice (tolerant)."""
    if not raw:
        return CallbackStatus.UNKNOWN
    return _STATUS_ALIASES.get(raw.strip().lower(), CallbackStatus.UNKNOWN)


def _require_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MalformedPayloadError("callback body must be a JSON object")
    return payload


def _as_object(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (tolerant nesting)."""
    return value if isinstance(value, dict) else {}


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    """Return the first present, non-empty string among ``keys`` (dotted ok)."""
    for key in keys:
        value: Any = payload
        for part in key.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _dedup_key(payload: dict[str, Any], task_id: str, status: CallbackStatus) -> str:
    explicit = _first_str(
        payload, "event_id", "delivery_id", "id", "request_id", "idempotency_key"
    )
    return explicit or f"{task_id}:{status.value}"


def canonical_parser(provider: str, payload: dict[str, Any]) -> ProviderCallback:
    """Parse our own already-canonical webhook shape (and the default fallback).

    Accepts ``task_id`` (or ``provider_task_id``), ``status``, optional
    ``asset_url`` / ``asset_kind`` / ``error_code`` / ``error_message`` /
    ``occurred_at`` / ``metadata``.
    """
    payload = _require_object(payload)
    task_id = _first_str(payload, "provider_task_id", "task_id", "taskId", "job_id")
    if not task_id:
        raise MalformedPayloadError("callback missing a task id")
    raw_status = _first_str(payload, "status", "state", "task_status")
    status = map_status(raw_status)
    occurred = payload.get("occurred_at")
    meta = payload.get("metadata")
    return ProviderCallback(
        provider=provider,
        provider_task_id=task_id,
        idempotency_key=_dedup_key(payload, task_id, status),
        status=status,
        asset_url=_first_str(payload, "asset_url", "video_url", "output_url", "url"),
        asset_kind=_first_str(payload, "asset_kind", "kind", "type"),
        error_code=_first_str(payload, "error_code", "code"),
        error_message=_first_str(payload, "error_message", "message", "error"),
        raw_status=raw_status,
        occurred_at=occurred if isinstance(occurred, str) else None,  # pydantic parses ISO str
        metadata=meta if isinstance(meta, dict) else {},
    )


def wan_parser(provider: str, payload: dict[str, Any]) -> ProviderCallback:
    """Parse a Wan / DashScope async-task callback.

    DashScope nests the task under ``output`` with ``task_id`` / ``task_status``
    (``SUCCEEDED`` / ``FAILED`` / ``RUNNING``) and the asset under
    ``output.results[0].url`` or ``output.video_url``.
    """
    payload = _require_object(payload)
    output: dict[str, Any] = _as_object(payload.get("output"))
    task_id = _first_str(payload, "task_id", "request_id") or _first_str(output, "task_id")
    if not task_id:
        raise MalformedPayloadError("wan callback missing task_id")
    raw_status = _first_str(payload, "task_status") or _first_str(output, "task_status")
    status = map_status(raw_status)
    asset_url = _first_str(output, "video_url", "url") or _result_url(output)
    code = _first_str(payload, "code") or _first_str(output, "code")
    message = _first_str(payload, "message") or _first_str(output, "message")
    merged: dict[str, Any] = {"task_id": task_id, **output}
    return ProviderCallback(
        provider=provider,
        provider_task_id=task_id,
        idempotency_key=_dedup_key(merged, task_id, status),
        status=status,
        asset_url=asset_url,
        asset_kind="video",
        error_code=code if status is CallbackStatus.FAILED else None,
        error_message=message if status is CallbackStatus.FAILED else None,
        raw_status=raw_status,
        metadata={},
    )


def _result_url(output: Any) -> str | None:
    if not isinstance(output, dict):
        return None
    results = output.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        url = results[0].get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def minimax_parser(provider: str, payload: dict[str, Any]) -> ProviderCallback:
    """Parse a MiniMax video-generation callback.

    MiniMax reports ``task_id`` + ``status`` and a ``file_id`` / ``download_url``,
    with a ``base_resp.status_code`` (``0`` = ok) for errors.
    """
    payload = _require_object(payload)
    task_id = _first_str(payload, "task_id", "taskId")
    if not task_id:
        raise MalformedPayloadError("minimax callback missing task_id")
    raw_status = _first_str(payload, "status", "task_status")
    status = map_status(raw_status)
    base: dict[str, Any] = _as_object(payload.get("base_resp"))
    err_code = base.get("status_code")
    return ProviderCallback(
        provider=provider,
        provider_task_id=task_id,
        idempotency_key=_dedup_key(payload, task_id, status),
        status=status,
        asset_url=_first_str(payload, "download_url", "video_url", "url"),
        asset_kind="video",
        error_code=str(err_code) if status is CallbackStatus.FAILED and err_code else None,
        error_message=_first_str(base, "status_msg"),
        raw_status=raw_status,
        metadata={"file_id": fid} if (fid := _first_str(payload, "file_id")) else {},
    )


#: The bespoke parsers, by provider slug. Any provider not here uses the
#: canonical parser, so internal services and new providers work immediately.
PARSERS: dict[str, Parser] = {
    "wan": wan_parser,
    "dashscope": wan_parser,
    "minimax": minimax_parser,
    "kinora": canonical_parser,
}


def parser_for(provider: str) -> Parser:
    """Return the parser for ``provider`` (the canonical parser as fallback)."""
    return PARSERS.get(provider, canonical_parser)


__all__ = [
    "PARSERS",
    "Parser",
    "canonical_parser",
    "map_status",
    "minimax_parser",
    "parser_for",
    "wan_parser",
]
