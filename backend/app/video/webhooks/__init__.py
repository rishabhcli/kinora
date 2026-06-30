"""Inbound webhook / callback ingress gateway for async video & audio providers.

A production HTTP door (``POST /api/video/webhooks/{provider}``) async media
providers post render-completion callbacks to. The subsystem owns the whole
untrusted-ingress problem so the rest of the platform never has to:

* **authentication** — per-provider HMAC / shared-secret signature verification
  with a timestamp anti-replay window (the signature *is* the auth; the route is
  unauthenticated by design) — :mod:`.signing`;
* **canonicalisation** — provider-specific payloads normalised into one
  :class:`~app.video.webhooks.models.ProviderCallback` — :mod:`.parsers`;
* **idempotency** — at-least-once deliveries collapsed to exactly-once
  processing — :mod:`.dedup`;
* **fast-ACK + handoff** — verified callbacks handed to a local
  :class:`~app.video.webhooks.models.JobCompletionSink` Protocol (the seam to the
  not-yet-merged async job lifecycle), with a poll/webhook **reconciler note** —
  :mod:`.gateway`;
* **guards + observability** — body-size + per-source rate limits
  (:mod:`.ratelimit`) and structured ingestion metrics (:mod:`.metrics`).

The FastAPI router (:mod:`.routes`) is appended to ``app.api.routes.ROUTERS``;
everything else is importable for the orchestrator to wire the real sink.
"""

from __future__ import annotations

from app.video.webhooks.dedup import DedupStore, InMemoryDedupStore
from app.video.webhooks.errors import (
    MalformedPayloadError,
    PayloadTooLargeError,
    ReplayError,
    SignatureError,
    UnknownProviderError,
    WebhookIngressError,
)
from app.video.webhooks.gateway import (
    IngestDisposition,
    IngestOutcome,
    WebhookGateway,
)
from app.video.webhooks.metrics import IngressMetrics
from app.video.webhooks.models import (
    CallbackStatus,
    JobCompletionSink,
    ProviderCallback,
)
from app.video.webhooks.routes import router
from app.video.webhooks.signing import (
    ProviderSigningConfig,
    SignatureVerifier,
    sign_body,
)

__all__ = [
    "CallbackStatus",
    "DedupStore",
    "InMemoryDedupStore",
    "IngestDisposition",
    "IngestOutcome",
    "IngressMetrics",
    "JobCompletionSink",
    "MalformedPayloadError",
    "PayloadTooLargeError",
    "ProviderCallback",
    "ProviderSigningConfig",
    "ReplayError",
    "SignatureError",
    "SignatureVerifier",
    "UnknownProviderError",
    "WebhookGateway",
    "WebhookIngressError",
    "router",
    "sign_body",
]
