"""Transcode-orchestration queue seam.

A narrow boundary so a worker can drain "derive these assets from this clip"
jobs without this package depending on Redis (the §12.1 priority queue is the
render domain's concern). The seam is a :class:`TranscodeQueue` Protocol plus an
:class:`InMemoryTranscodeQueue` reference implementation used by tests and the
in-process default; a real deployment can back it with the existing Redis queue
by implementing the same three methods.

A :class:`TranscodeJob` names the source asset and which derivations to produce
(poster / thumbnail / sprite / HLS / DASH). The :class:`app.media.service.MediaService`
is what actually executes a job (ffmpeg + store + register); the queue only
carries the request, so the worker stays a thin loop.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Derivation(StrEnum):
    """A derivation a transcode job can request from a source clip/scene."""

    POSTER = "poster"
    THUMBNAIL = "thumbnail"
    SPRITE = "sprite"
    HLS = "hls"
    DASH = "dash"


#: The default derivation set for a freshly-accepted film (everything the
#: reading-room player wants): a poster, a scrubber sprite sheet, and HLS.
DEFAULT_DERIVATIONS: tuple[Derivation, ...] = (
    Derivation.POSTER,
    Derivation.SPRITE,
    Derivation.HLS,
)


@dataclass(frozen=True, slots=True)
class TranscodeJob:
    """A request to derive assets from one stored source blob."""

    source_key: str
    derivations: tuple[Derivation, ...] = DEFAULT_DERIVATIONS
    book_id: str | None = None
    #: Opaque id for idempotency / status correlation.
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def with_id(self, job_id: str) -> TranscodeJob:
        """Return a copy with an explicit ``job_id`` (for idempotency keys)."""
        return TranscodeJob(
            source_key=self.source_key,
            derivations=self.derivations,
            book_id=self.book_id,
            job_id=job_id,
        )


@runtime_checkable
class TranscodeQueue(Protocol):
    """The minimal enqueue/drain seam a transcode worker needs."""

    async def enqueue(self, job: TranscodeJob) -> str:
        """Submit a job; returns its id."""
        ...

    async def dequeue(self) -> TranscodeJob | None:
        """Pop the next job, or ``None`` when the queue is empty."""
        ...

    async def depth(self) -> int:
        """Number of jobs currently waiting."""
        ...


class InMemoryTranscodeQueue:
    """A FIFO in-memory queue (default + test double for the seam).

    De-duplicates by ``job_id`` so an at-least-once enqueue does not double a
    job — the same property a Redis-backed implementation would provide via a
    set membership check.
    """

    def __init__(self) -> None:
        self._jobs: deque[TranscodeJob] = deque()
        self._seen: set[str] = set()

    async def enqueue(self, job: TranscodeJob) -> str:
        if job.job_id in self._seen:
            return job.job_id
        self._seen.add(job.job_id)
        self._jobs.append(job)
        return job.job_id

    async def dequeue(self) -> TranscodeJob | None:
        if not self._jobs:
            return None
        return self._jobs.popleft()

    async def depth(self) -> int:
        return len(self._jobs)


__all__ = [
    "DEFAULT_DERIVATIONS",
    "Derivation",
    "InMemoryTranscodeQueue",
    "TranscodeJob",
    "TranscodeQueue",
]
