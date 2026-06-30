"""The webhook ingress gateway — verify, parse, dedup, fast-ACK, hand off.

This is the heart of the subsystem: a provider-agnostic pipeline a thin FastAPI
route drives. For one inbound callback it:

1. **guards the body size** (a slow-loris / oversized-body defence) — 413;
2. **authenticates** via :class:`SignatureVerifier` (HMAC / shared-secret +
   timestamp anti-replay) — 401, distinguishing forged from stale;
3. **parses** the verified body into a canonical :class:`ProviderCallback` — 422
   if unusable, but *tolerant* of unknown statuses and unknown task ids;
4. **dedups** on the callback's ``dedup_key`` so an at-least-once provider's
   redelivery is processed exactly once;
5. **hands off** the callback to the :class:`JobCompletionSink` and returns a
   :class:`IngestOutcome` telling the route to **fast-ACK** (202).

The actual durable work (resolve the job, persist the asset, advance the state
machine, charge/release budget) happens in the sink, *after* the HTTP response —
the gateway only schedules it. That keeps the handler fast so the provider's
delivery timeout never trips and triggers a redelivery storm.

────────────────────────────────────────────────────────────────────────────
RECONCILER NOTE (poll/webhook race — kinora.md §12.1)
────────────────────────────────────────────────────────────────────────────
A render task can complete via **two** independent paths: this webhook *and* the
render worker's polling loop on the Wan async task id. They race, and a webhook
can even arrive *before* the job row commits (out-of-order). This gateway's job
is only to make the webhook path correct in isolation:

* idempotency (dedup_key) means whichever path wins first, the loser is a no-op;
* an unknown ``provider_task_id`` is **tolerated** — the sink returns without
  raising, and a periodic *reconciler* (owned by the job engine, not this
  subsystem) sweeps tasks that are terminal-by-poll but never saw a webhook (and
  vice-versa), using the same dedup key so it, too, can't double-apply.

The reconciler is intentionally NOT built here — it belongs to the job lifecycle
the orchestrator wires to the sink. This note records the contract it must honour.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import get_logger
from app.video.webhooks.dedup import DedupStore, InMemoryDedupStore
from app.video.webhooks.errors import (
    MalformedPayloadError,
    PayloadTooLargeError,
    SignatureError,
    UnknownProviderError,
)
from app.video.webhooks.metrics import IngressMetrics
from app.video.webhooks.models import CallbackStatus, JobCompletionSink, ProviderCallback
from app.video.webhooks.parsers import parser_for
from app.video.webhooks.payload import decode_json
from app.video.webhooks.signing import SignatureVerifier

logger = get_logger("app.video.webhooks.gateway")

#: 1 MiB default ceiling for an inbound callback body. Provider callbacks are
#: small JSON status docs; anything larger is rejected before we read/parse it.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024


class IngestDisposition(StrEnum):
    """What the gateway did with a (post-verification) callback."""

    #: First sighting — handed to the sink. Route ⇒ 202.
    ACCEPTED = "accepted"
    #: A duplicate delivery, collapsed by idempotency. Route ⇒ 202 (still an ACK).
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """The result of admitting one verified callback (returned to the route)."""

    disposition: IngestDisposition
    callback: ProviderCallback

    @property
    def processed(self) -> bool:
        """Whether this call handed the callback to the sink (vs. deduped)."""
        return self.disposition is IngestDisposition.ACCEPTED


@dataclass
class WebhookGateway:
    """Verify + parse + dedup inbound callbacks and hand them to the sink."""

    verifier: SignatureVerifier
    sink: JobCompletionSink
    dedup: DedupStore = None  # type: ignore[assignment]
    metrics: IngressMetrics = None  # type: ignore[assignment]
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    def __post_init__(self) -> None:
        if self.dedup is None:
            self.dedup = InMemoryDedupStore()
        if self.metrics is None:
            self.metrics = IngressMetrics()

    def check_size(self, body: bytes, content_length: int | None = None) -> None:
        """Reject an oversized body (413) — checks the header *and* the bytes.

        The advertised ``Content-Length`` is checked first so we can bail before
        materialising a huge body, and the actual byte length is checked too
        (a lying/absent header can't slip past).
        """
        if content_length is not None and content_length > self.max_body_bytes:
            self.metrics.too_large += 1
            raise PayloadTooLargeError(
                f"content-length {content_length} exceeds {self.max_body_bytes} byte cap"
            )
        if len(body) > self.max_body_bytes:
            self.metrics.too_large += 1
            raise PayloadTooLargeError(
                f"body of {len(body)} bytes exceeds {self.max_body_bytes} byte cap"
            )

    def authenticate(self, provider: str, body: bytes, headers: dict[str, str]) -> None:
        """Verify the callback's signature + freshness; raise + count on failure."""
        try:
            self.verifier.verify(provider, body, headers)
        except UnknownProviderError:
            self.metrics.unknown_provider += 1
            raise
        except SignatureError as exc:
            # ReplayError is a SignatureError subtype; split the counters so a
            # stale delivery is distinguishable from a forgery in metrics/logs.
            from app.video.webhooks.errors import ReplayError

            if isinstance(exc, ReplayError):
                self.metrics.replays += 1
                logger.warning("video.webhook.replay_rejected", provider=provider, reason=str(exc))
            else:
                self.metrics.bad_signature += 1
                logger.warning("video.webhook.bad_signature", provider=provider, reason=str(exc))
            raise

    def parse(self, provider: str, body: bytes) -> ProviderCallback:
        """Decode + normalise a verified body into a canonical callback (422 on bad)."""
        try:
            payload = decode_json(body)
            callback = parser_for(provider)(provider, payload)
        except MalformedPayloadError:
            self.metrics.malformed += 1
            raise
        if callback.status is CallbackStatus.UNKNOWN:
            self.metrics.unknown_status += 1
            logger.info(
                "video.webhook.unknown_status",
                provider=provider,
                task_id=callback.provider_task_id,
                raw_status=callback.raw_status,
            )
        return callback

    async def admit(self, callback: ProviderCallback) -> IngestOutcome:
        """Dedup + (if first) hand off to the sink. Always an ACK-able outcome.

        The handoff is awaited here so a *test* sees the sink invoked
        deterministically; the route still returns fast because the sink itself
        is the place that may defer heavy work, and the route can also wrap this
        in a background task. A sink exception is logged and swallowed so a sink
        hiccup never turns into an unacknowledged provider retry.
        """
        first = await self.dedup.claim(callback.dedup_key)
        if not first:
            self.metrics.duplicates += 1
            logger.info(
                "video.webhook.duplicate",
                provider=callback.provider,
                task_id=callback.provider_task_id,
                dedup_key=callback.dedup_key,
            )
            return IngestOutcome(IngestDisposition.DUPLICATE, callback)

        self.metrics.accepted += 1
        self.metrics.by_provider[callback.provider] += 1
        logger.info(
            "video.webhook.accepted",
            provider=callback.provider,
            task_id=callback.provider_task_id,
            status=callback.status.value,
            has_asset=callback.asset_url is not None,
        )
        await self._deliver(callback)
        return IngestOutcome(IngestDisposition.ACCEPTED, callback)

    async def _deliver(self, callback: ProviderCallback) -> None:
        try:
            await self.sink.on_callback(callback)
        except Exception:  # noqa: BLE001 - never let a sink failure 5xx the ACK
            logger.exception(
                "video.webhook.sink_error",
                provider=callback.provider,
                task_id=callback.provider_task_id,
            )

    def schedule(self, callback: ProviderCallback) -> None:
        """Fire-and-forget the sink handoff on the running loop (true fast-ACK).

        The route can call :meth:`admit` (awaited, deterministic) or, for the
        absolute fastest ACK under load, claim+``schedule`` so the HTTP response
        is sent before the sink runs. Exposed for the orchestrator's wiring.
        """
        asyncio.get_running_loop().create_task(self._deliver(callback))


__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "IngestDisposition",
    "IngestOutcome",
    "WebhookGateway",
]
