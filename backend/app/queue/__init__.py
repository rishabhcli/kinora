"""The Redis-backed priority render queue + worker (kinora.md §12.1–§12.3).

Three lanes (committed > speculative > keyframe) over Redis sorted sets, with
content-hash idempotency/dedup, depth-based backpressure, cooperative
cancellation tokens, exponential-backoff retries, and a dead-letter path. The
:class:`RedisRenderEnqueuer` is the DI seam Phase 9's MCP ``shot.render`` injects
into the memory layer; :class:`RenderWorker` is the async consumer that drives
the real :class:`app.render.pipeline.RenderPipeline`.
"""

from __future__ import annotations

from app.queue.enqueuer import RedisRenderEnqueuer
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
    "EnqueueResult",
    "QueueStats",
    "QueuedJob",
    "RedisRenderEnqueuer",
    "RedisRenderQueue",
    "RenderWorker",
    "RetryDecision",
    "RetryOutcome",
    "book_channel",
    "book_progress_key",
    "library_channel",
    "session_channel",
]
