"""Unified async video-job lifecycle engine (provider-agnostic).

Hosted video models are asynchronous: you *submit* a render, *poll* (or receive a
*webhook*) until it finishes, then get a result URL that **expires** and must be
downloaded + persisted immediately. This package models that whole lifecycle once,
for any provider, as a durable :class:`VideoJob` driven by a single
:class:`VideoJobEngine`.

What it provides
----------------
* A durable :class:`VideoJob` aggregate (states ``submitted → running →
  succeeded/failed/cancelled/expired``) with pure transition predicates.
* A :class:`VideoJobRepository` seam — an :class:`InMemoryVideoJobRepository`
  (tests + single-process dev) and a :class:`DatabaseVideoJobRepository` sketch.
* A poller with provider-specific backoff schedules (:mod:`.schedules`) and an
  overall deadline, **plus** webhook-based completion (:class:`HmacWebhookVerifier`
  signature check) reconciled against polling so neither double-processes.
* **Eager** asset download to object storage on success, with sha256 + retry
  (:class:`AssetPersister`).
* Crash recovery (:meth:`VideoJobEngine.recover_inflight`), idempotency keys, and
  a cancellation path.
* Lifecycle :class:`JobEvent`s + metrics through injectable sinks.

Clean async API: :meth:`VideoJobEngine.submit`,
:meth:`~VideoJobEngine.await_result`, :meth:`~VideoJobEngine.handle_webhook`,
:meth:`~VideoJobEngine.recover_inflight`, :meth:`~VideoJobEngine.cancel`.

Everything is wired through protocols (clock, provider, store, sinks), so the
entire engine runs deterministically in tests with a fake clock + in-memory store
+ a scripted provider — no infra, no network, no real spend.
"""

from __future__ import annotations

from .assets import AssetPersister, AssetPersistError, PersistConfig
from .clock import ManualClock, SystemClock
from .engine import VideoJobEngine
from .events import JobEvent, JobEventType
from .models import (
    INFLIGHT_STATES,
    TERMINAL_STATES,
    JobAsset,
    JobRequest,
    JobState,
    JobTransitionError,
    VideoJob,
)
from .observability import (
    NullMetricsSink,
    RecordingEventSink,
    RecordingMetricsSink,
    StructlogEventSink,
)
from .ports import (
    AssetFetcher,
    EventSink,
    JobClock,
    MetricsSink,
    ObjectStorePort,
    PollSchedule,
    ProviderPoll,
    ProviderStatus,
    ProviderSubmit,
    VideoJobProvider,
    WebhookResult,
    WebhookVerifier,
)
from .repository import (
    DatabaseVideoJobRepository,
    InMemoryVideoJobRepository,
    StaleJobVersionError,
    VideoJobRepository,
)
from .schedules import (
    DASHSCOPE_PROFILE,
    DEFAULT_PROFILE,
    MINIMAX_PROFILE,
    PollProfile,
    profile_for,
)
from .webhook import AllowAllVerifier, HmacWebhookVerifier, clip_storage_key

__all__ = [
    "DASHSCOPE_PROFILE",
    "DEFAULT_PROFILE",
    "INFLIGHT_STATES",
    "MINIMAX_PROFILE",
    "TERMINAL_STATES",
    "AllowAllVerifier",
    "AssetFetcher",
    "AssetPersistError",
    "AssetPersister",
    "DatabaseVideoJobRepository",
    "EventSink",
    "HmacWebhookVerifier",
    "InMemoryVideoJobRepository",
    "JobAsset",
    "JobClock",
    "JobEvent",
    "JobEventType",
    "JobRequest",
    "JobState",
    "JobTransitionError",
    "ManualClock",
    "MetricsSink",
    "NullMetricsSink",
    "ObjectStorePort",
    "PersistConfig",
    "PollProfile",
    "PollSchedule",
    "ProviderPoll",
    "ProviderStatus",
    "ProviderSubmit",
    "RecordingEventSink",
    "RecordingMetricsSink",
    "StaleJobVersionError",
    "StructlogEventSink",
    "SystemClock",
    "VideoJob",
    "VideoJobEngine",
    "VideoJobProvider",
    "VideoJobRepository",
    "WebhookResult",
    "WebhookVerifier",
    "clip_storage_key",
    "profile_for",
]
