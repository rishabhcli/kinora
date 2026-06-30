"""HTTP ingress for async video/audio provider callbacks.

One route — ``POST /api/video/webhooks/{provider}`` — is the production door an
async media provider knocks on when a render task finishes. It is deliberately
**unauthenticated** (no bearer): the provider is a machine holding only a shared
signing secret, so the *signature is the authentication*. The handler is a thin
shell over :class:`~app.video.webhooks.gateway.WebhookGateway`; the route's only
jobs are size/rate guards, mapping gateway errors to status codes, and a fast ACK.

Status codes:

* **202 Accepted** — the callback verified, parsed, and was handed to the sink
  (or was a deduplicated duplicate; both are a legitimate ACK so the provider
  stops retrying).
* **401 Unauthorized** — bad/missing signature, or a stale (replayed) timestamp.
* **404 Not Found** — no signing config registered for that provider slug.
* **413 Payload Too Large** — body over the ingress size cap.
* **422 Unprocessable Entity** — verified but unparseable body.
* **429 Too Many Requests** — per-source rate limit tripped.

A ``GET /api/video/webhooks/_metrics`` companion exposes the §12.5 ingestion
counters for the demo metrics panel / a scrape.

The gateway (verifier built from settings + an in-process dedup store, metrics,
rate limiter, and the default logging sink) is built once per app and cached on
``app.state``; the orchestrator swaps in the real job-engine sink later by
setting ``app.state.video_webhook_gateway`` before the first request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status

from app.api.errors import APIError
from app.core.logging import get_logger
from app.video.webhooks.config import LoggingJobCompletionSink, build_verifier
from app.video.webhooks.errors import (
    MalformedPayloadError,
    PayloadTooLargeError,
    ReplayError,
    SignatureError,
    UnknownProviderError,
)
from app.video.webhooks.gateway import WebhookGateway
from app.video.webhooks.ratelimit import TokenBucketRateLimiter

logger = get_logger("app.api.video_webhooks")

router = APIRouter(prefix="/video/webhooks", tags=["video-webhooks"])

#: ``app.state`` attribute the (cached) gateway + limiter live under.
_GATEWAY_ATTR = "video_webhook_gateway"
_LIMITER_ATTR = "video_webhook_ratelimit"


def _gateway(request: Request) -> WebhookGateway:
    """Return the per-app gateway, building + caching it on first use.

    Built from ``container.settings`` so per-provider secrets and the replay
    tolerance come from configuration. If the orchestrator pre-set
    ``app.state.video_webhook_gateway`` (with the real job-engine sink) we use
    that instead — the wiring seam.
    """
    existing = getattr(request.app.state, _GATEWAY_ATTR, None)
    if isinstance(existing, WebhookGateway):
        return existing
    container = getattr(request.app.state, "container", None)
    settings = getattr(container, "settings", None)
    verifier = build_verifier(settings) if settings is not None else build_verifier(object())
    max_body = int(getattr(settings, "video_webhook_max_body_bytes", 1 * 1024 * 1024))
    gateway = WebhookGateway(
        verifier=verifier,
        sink=LoggingJobCompletionSink(),
        max_body_bytes=max_body,
    )
    setattr(request.app.state, _GATEWAY_ATTR, gateway)
    return gateway


def _limiter(request: Request) -> TokenBucketRateLimiter:
    existing = getattr(request.app.state, _LIMITER_ATTR, None)
    if isinstance(existing, TokenBucketRateLimiter):
        return existing
    container = getattr(request.app.state, "container", None)
    settings = getattr(container, "settings", None)
    limiter = TokenBucketRateLimiter(
        capacity=int(getattr(settings, "video_webhook_rate_capacity", 120)),
        refill_per_s=float(getattr(settings, "video_webhook_rate_refill_per_s", 4.0)),
    )
    setattr(request.app.state, _LIMITER_ATTR, limiter)
    return limiter


def _client_key(request: Request, provider: str) -> str:
    client = request.client
    host = client.host if client is not None else "unknown"
    return f"{provider}:{host}"


@router.post("/{provider}", status_code=status.HTTP_202_ACCEPTED)
async def receive_callback(provider: str, request: Request) -> dict[str, str]:
    """Receive, verify, parse, dedup, and hand off one async-provider callback.

    Returns a 202 on a verified callback (new or deduplicated). Every rejection
    is a typed :class:`APIError` rendered in the gateway envelope with the right
    status; the signature is checked before the body is parsed, and the size
    guard before anything is read into the verifier.
    """
    gateway = _gateway(request)
    gateway.metrics.received += 1

    # Per-source throttle (unauthenticated route ⇒ guard before doing work).
    if not _limiter(request).allow(_client_key(request, provider)):
        raise APIError(
            "rate_limited",
            "too many callbacks; slow down",
            status=429,
            detail={"provider": provider},
        )

    # Size guard using the advertised Content-Length first, then the real bytes.
    content_length = _content_length(request)
    body = await request.body()
    try:
        gateway.check_size(body, content_length)
    except PayloadTooLargeError as exc:
        raise APIError("payload_too_large", str(exc), status=413) from exc

    # Authenticate: forged → 401, stale replay → 401 (distinct log/metric),
    # unknown provider → 404.
    try:
        gateway.authenticate(provider, body, dict(request.headers))
    except UnknownProviderError as exc:
        raise APIError("unknown_provider", str(exc), status=404) from exc
    except ReplayError as exc:
        raise APIError("stale_callback", str(exc), status=401) from exc
    except SignatureError as exc:
        raise APIError("bad_signature", str(exc), status=401) from exc

    # Parse into the canonical shape (tolerant of unknown status / task id).
    try:
        callback = gateway.parse(provider, body)
    except MalformedPayloadError as exc:
        raise APIError("malformed_payload", str(exc), status=422) from exc

    # Dedup + fast-ACK handoff to the sink.
    outcome = await gateway.admit(callback)
    return {
        "status": "accepted",
        "provider": provider,
        "task_id": callback.provider_task_id,
        "disposition": outcome.disposition.value,
    }


@router.get("/_metrics")
async def ingress_metrics(request: Request) -> dict[str, Any]:
    """Expose the §12.5 callback-ingestion counters (demo panel / scrape)."""
    gateway = _gateway(request)
    return {"providers": gateway.verifier.providers(), "metrics": gateway.metrics.snapshot()}


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


__all__ = ["router"]
