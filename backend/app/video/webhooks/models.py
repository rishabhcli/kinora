"""Canonical webhook domain types + the local ``JobCompletionSink`` seam.

Every async video/audio provider has its own callback wire-format. The gateway
*normalises* each into one provider-agnostic :class:`ProviderCallback` so the rest
of the platform (the job engine, the reconciler, metrics) never has to learn a
new shape per provider. This module owns:

* :class:`CallbackStatus` — the canonical terminal/non-terminal status lattice an
  async render task can be in, mapped from each provider's vocabulary.
* :class:`ProviderCallback` — the normalised, validated callback the sink sees.
* :class:`JobCompletionSink` — a **local** :class:`typing.Protocol` the route hands
  a verified callback to. Rounds 1 & 2 (the real async job lifecycle) are not
  merged here, so this gateway defines the minimal contract it needs; the
  orchestrator wires the concrete job engine to it later. Keeping it a Protocol
  (not an import) is what lets this subsystem ship and be tested in isolation.

Nothing in here does I/O — these are pure value types + a structural interface,
so they unit-test with no infra and no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class CallbackStatus(StrEnum):
    """The canonical state an async render task is reported in.

    Providers use wildly different words (``SUCCEEDED`` / ``done`` / ``complete``
    / ``finished``; ``FAILED`` / ``error`` / ``cancelled``). The parsers collapse
    each provider's vocabulary onto this lattice so downstream code branches on a
    single, stable enum. ``UNKNOWN`` is deliberate: an unrecognised provider
    status is *tolerated* (logged + handed off as ``UNKNOWN``), never a hard
    error, because providers add states over time and a callback gateway must not
    fall over on a status string it has not seen yet.
    """

    #: The task finished and an asset (video/audio) is available.
    SUCCEEDED = "succeeded"
    #: The task failed terminally; downstream drops to the degradation ladder.
    FAILED = "failed"
    #: The task was cancelled (e.g. the reader seeked away) — terminal, no asset.
    CANCELLED = "cancelled"
    #: A non-terminal progress ping ("running"/"queued"/"processing").
    RUNNING = "running"
    #: A status string the parser did not recognise — tolerated, not rejected.
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Whether this status ends the task's lifecycle (no more callbacks expected)."""
        return self in (CallbackStatus.SUCCEEDED, CallbackStatus.FAILED, CallbackStatus.CANCELLED)


class ProviderCallback(BaseModel):
    """A verified, normalised inbound callback from an async media provider.

    This is the single shape the :class:`JobCompletionSink` receives regardless of
    which provider delivered it. ``extra="forbid"`` is intentional on the *parsed*
    canonical model so a parser bug that leaks a raw provider field is caught in
    tests rather than silently propagated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The provider slug from the URL path (``wan`` / ``minimax`` / ``dashscope``…).
    provider: str = Field(min_length=1, max_length=64)
    #: The provider's own task id (its primary key for the async job). The gateway
    #: tolerates an *unknown* task id — see the reconciler note in ``gateway``.
    provider_task_id: str = Field(min_length=1, max_length=512)
    #: The provider's idempotency / delivery id when it sends one; the gateway
    #: dedups on ``(provider, idempotency_key)`` and falls back to the task id +
    #: status when a provider omits a per-delivery id.
    idempotency_key: str = Field(min_length=1, max_length=512)
    #: The canonical lifecycle status.
    status: CallbackStatus
    #: A direct URL to the produced asset (only set on ``SUCCEEDED``); the render
    #: pipeline persists it to object storage because provider URLs expire.
    asset_url: str | None = Field(default=None, max_length=4096)
    #: ``video`` / ``audio`` — the asset kind, when the provider distinguishes it.
    asset_kind: str | None = Field(default=None, max_length=32)
    #: A provider error code/message when ``status == FAILED`` (never a secret).
    error_code: str | None = Field(default=None, max_length=256)
    error_message: str | None = Field(default=None, max_length=2048)
    #: The provider's own status string, verbatim, for debugging an UNKNOWN.
    raw_status: str | None = Field(default=None, max_length=128)
    #: When the provider stamped the event (its clock), if present.
    occurred_at: datetime | None = None
    #: When the gateway accepted the callback (our clock) — set at parse time.
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: A small bag of provider-specific extras the sink may want (never secrets).
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """The stable key the gateway dedups deliveries on.

        ``(provider, idempotency_key)`` — a provider's at-least-once delivery of
        the *same* event collapses to one processing pass. Distinct status
        transitions for one task (``running`` → ``succeeded``) carry different
        idempotency keys (each parser derives one that includes the status when
        the provider gives no per-delivery id) so they are processed separately.
        """
        return f"{self.provider}:{self.idempotency_key}"


@runtime_checkable
class JobCompletionSink(Protocol):
    """Where a verified callback is handed off for the *real* work.

    This is the seam between this HTTP-ingress gateway and the (not-yet-merged)
    async job lifecycle. The route fast-ACKs and schedules
    :meth:`on_callback` so the HTTP handler returns quickly; the sink does the
    durable work (resolve the job by ``provider_task_id``, persist the asset,
    advance the state machine, release/charge budget, fan out a render-done
    event). It is a structural Protocol so any concrete engine that grows this
    method satisfies it with no import coupling.

    Contract:
    * It must be **idempotent on its own side too** — the gateway dedups, but a
      crash-after-ACK / before-persist still warrants a belt-and-braces guard.
    * An *unknown* ``provider_task_id`` must be tolerated (return, don't raise):
      the callback may have arrived before the job row committed (out-of-order),
      or for a task this node never created. The reconciler closes that race.
    * It should not raise for normal outcomes; raising is reserved for genuine
      infra failure and is logged + swallowed by the async handoff so a sink
      hiccup never turns into an unacknowledged provider retry storm.
    """

    async def on_callback(self, callback: ProviderCallback) -> None:
        """Durably process one verified, deduplicated provider callback."""
        ...


__all__ = ["CallbackStatus", "JobCompletionSink", "ProviderCallback"]
