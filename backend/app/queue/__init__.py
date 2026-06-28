"""The Redis-backed priority render queue + worker (kinora.md §12.1–§12.3).

Three lanes (committed > speculative > keyframe) over Redis sorted sets, with
content-hash idempotency/dedup, depth-based backpressure, cooperative
cancellation tokens, exponential-backoff retries, and a dead-letter path. The
:class:`RedisRenderEnqueuer` is the DI seam Phase 9's MCP ``shot.render`` injects
into the memory layer; :class:`RenderWorker` is the async consumer that drives
the real :class:`app.render.pipeline.RenderPipeline`.

Production-grade extensions layered on the core queue (each independently
testable against :mod:`app.queue.fakeredis`, no infra):

* :mod:`app.queue.backoff` — exponential backoff with jitter (full/equal/decorrelated).
* :mod:`app.queue.admission` — depth backpressure + per-session fairness (§12.2).
* :mod:`app.queue.dlq` — dead-letter inspect / replay / purge tooling (§12.1).
* :mod:`app.queue.leases` — lease-renewal guard + standalone reaper (§12.1).
* :mod:`app.queue.autoscale` — depth-driven worker-pool autoscaling (§4.9/§12.2).
* :mod:`app.queue.fakeredis` — in-process async Redis double for the test harness.
"""

from __future__ import annotations

from app.queue.admission import (
    AdmissionController,
    AdmissionDecision,
    AdmissionReason,
    SessionFairness,
)
from app.queue.autoscale import AutoscalePlan, LaneAutoscaler, LanePolicy
from app.queue.backoff import BackoffSchedule, JitterStrategy
from app.queue.dlq import DeadLetterEntry, DeadLetterQueue, DeadLetterStats
from app.queue.enqueuer import RedisRenderEnqueuer
from app.queue.leases import LeaseGuard, Reaper
from app.queue.redis_queue import (
    EnqueueResult,
    QueuedJob,
    QueueStats,
    RedisRenderQueue,
    RetryDecision,
    RetryOutcome,
    book_channel,
    book_progress_key,
    library_channel,
    session_channel,
)
from app.queue.worker import RenderWorker

__all__ = [
    "AdmissionController",
    "AdmissionDecision",
    "AdmissionReason",
    "AutoscalePlan",
    "BackoffSchedule",
    "DeadLetterEntry",
    "DeadLetterQueue",
    "DeadLetterStats",
    "EnqueueResult",
    "JitterStrategy",
    "LaneAutoscaler",
    "LanePolicy",
    "LeaseGuard",
    "QueueStats",
    "QueuedJob",
    "Reaper",
    "RedisRenderEnqueuer",
    "RedisRenderQueue",
    "RenderWorker",
    "RetryDecision",
    "RetryOutcome",
    "SessionFairness",
    "book_channel",
    "book_progress_key",
    "library_channel",
    "session_channel",
]
