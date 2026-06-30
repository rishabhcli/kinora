"""Durable persistence for :class:`VideoJob` — the seam that makes the lifecycle
survive a crash.

Two things matter for correctness and both live here:

* **Idempotency** — :meth:`VideoJobRepository.upsert_by_idempotency_key` is the
  single place a re-submit is collapsed onto its existing job, so the same
  ``(provider, idempotency_key)`` never spawns two provider tasks.
* **Optimistic concurrency** — :meth:`save` rejects a write whose ``version`` is
  stale, so a webhook and a poll racing to terminalize the same job cannot both
  win (the loser sees :class:`StaleJobVersionError` and reconciles).

This module ships the in-memory implementation (used everywhere in tests, and a
fine fit for a single-process dev run) plus :class:`DatabaseVideoJobRepository`,
an abstract sketch documenting exactly what a SQLAlchemy-backed implementation
must provide (the real table is a separate migration task, intentionally out of
scope here).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .models import INFLIGHT_STATES, VideoJob


class StaleJobVersionError(RuntimeError):
    """Raised by :meth:`VideoJobRepository.save` on an optimistic-lock conflict."""

    def __init__(self, *, job_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"stale video-job write (job={job_id} expected base version "
            f"{expected}, store has {actual})"
        )
        self.job_id = job_id
        self.expected = expected
        self.actual = actual


@runtime_checkable
class VideoJobRepository(Protocol):
    """The persistence contract the engine depends on."""

    async def get(self, job_id: str) -> VideoJob | None:
        """Fetch a job by id, or ``None``."""
        ...

    async def get_by_idempotency_key(self, provider: str, key: str) -> VideoJob | None:
        """Fetch the job a ``(provider, idempotency_key)`` already maps to."""
        ...

    async def upsert_by_idempotency_key(self, job: VideoJob) -> tuple[VideoJob, bool]:
        """Insert ``job``, or return the existing one for its idempotency key.

        Returns ``(stored_job, created)`` where ``created`` is ``False`` when an
        existing job was returned (the dedup case). Must be atomic so concurrent
        submits collapse to a single winner.
        """
        ...

    async def save(self, job: VideoJob) -> VideoJob:
        """Persist a *new snapshot* of an existing job under optimistic locking.

        ``job.version`` is the post-mutation version (already bumped by the
        ``with_*`` helper). The write succeeds only if the store still holds
        ``job.version - 1``; otherwise :class:`StaleJobVersionError` is raised.
        """
        ...

    async def list_inflight(self, *, provider: str | None = None) -> list[VideoJob]:
        """Return every job still in an in-flight state (for crash recovery)."""
        ...

    async def find_by_provider_task_id(
        self, provider: str, provider_task_id: str
    ) -> VideoJob | None:
        """Resolve a job from a provider task id (the webhook correlation path)."""
        ...


class InMemoryVideoJobRepository:
    """A thread/async-safe in-memory :class:`VideoJobRepository`.

    Backed by plain dicts under a single :class:`asyncio.Lock`, so idempotency
    upserts and optimistic-locked saves are linearizable within one event loop —
    exactly the guarantees the engine relies on, with zero infra. The lock makes
    the webhook/poll race deterministic in tests.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, VideoJob] = {}
        self._idem: dict[tuple[str, str], str] = {}  # (provider, key) -> job_id
        self._by_task: dict[tuple[str, str], str] = {}  # (provider, task_id) -> job_id
        self._lock = asyncio.Lock()

    async def get(self, job_id: str) -> VideoJob | None:
        async with self._lock:
            return self._by_id.get(job_id)

    async def get_by_idempotency_key(self, provider: str, key: str) -> VideoJob | None:
        async with self._lock:
            job_id = self._idem.get((provider, key))
            return self._by_id.get(job_id) if job_id else None

    async def upsert_by_idempotency_key(self, job: VideoJob) -> tuple[VideoJob, bool]:
        async with self._lock:
            key = job.request.idempotency_key
            if key is not None:
                existing_id = self._idem.get((job.provider, key))
                if existing_id is not None:
                    return self._by_id[existing_id], False
                self._idem[(job.provider, key)] = job.id
            self._by_id[job.id] = job
            self._reindex_task(job)
            return job, True

    async def save(self, job: VideoJob) -> VideoJob:
        async with self._lock:
            current = self._by_id.get(job.id)
            if current is not None and current.version != job.version - 1:
                raise StaleJobVersionError(
                    job_id=job.id, expected=job.version - 1, actual=current.version
                )
            self._by_id[job.id] = job
            self._reindex_task(job)
            return job

    async def list_inflight(self, *, provider: str | None = None) -> list[VideoJob]:
        async with self._lock:
            jobs = [
                j
                for j in self._by_id.values()
                if j.state in INFLIGHT_STATES and (provider is None or j.provider == provider)
            ]
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    async def find_by_provider_task_id(
        self, provider: str, provider_task_id: str
    ) -> VideoJob | None:
        async with self._lock:
            job_id = self._by_task.get((provider, provider_task_id))
            return self._by_id.get(job_id) if job_id else None

    def _reindex_task(self, job: VideoJob) -> None:
        if job.provider_task_id is not None:
            self._by_task[(job.provider, job.provider_task_id)] = job.id


class DatabaseVideoJobRepository(ABC, VideoJobRepository):
    """Abstract sketch of a SQLAlchemy-backed :class:`VideoJobRepository`.

    A concrete implementation (a separate migration + ORM-model task) would back
    each method with one statement against a ``video_jobs`` table shaped like::

        video_jobs(
            id              text primary key,
            provider        text not null,
            state           text not null,                  -- JobState value
            provider_task_id text,
            idempotency_key text,
            request         jsonb not null,                 -- JobRequest payload
            asset           jsonb,                          -- JobAsset, when SUCCEEDED
            deadline_at     double precision,
            poll_attempts   integer not null default 0,
            download_attempts integer not null default 0,
            error           text,
            completed_by    text,
            version         integer not null default 0,
            created_at      double precision not null,
            updated_at      double precision not null,
            unique (provider, idempotency_key),             -- enforces dedup
            unique (provider, provider_task_id)             -- enforces correlation
        )
        -- partial index for fast crash-recovery scans:
        create index on video_jobs (provider) where state in ('submitted','running');

    Required guarantees mapped onto SQL:

    * :meth:`upsert_by_idempotency_key` → ``INSERT ... ON CONFLICT
      (provider, idempotency_key) DO NOTHING RETURNING *`` then re-select on the
      no-row case; the unique constraint makes concurrent submits collapse.
    * :meth:`save` → ``UPDATE ... SET ..., version = :new WHERE id = :id AND
      version = :new - 1``; ``rowcount == 0`` ⇒ :class:`StaleJobVersionError`.
    * :meth:`list_inflight` → ``SELECT ... WHERE state IN
      ('submitted','running')`` (hits the partial index).

    Kept abstract on purpose: the engine is fully exercised against
    :class:`InMemoryVideoJobRepository`; the DB binding is additive and
    migration-gated.
    """

    @abstractmethod
    async def get(self, job_id: str) -> VideoJob | None: ...

    @abstractmethod
    async def get_by_idempotency_key(self, provider: str, key: str) -> VideoJob | None: ...

    @abstractmethod
    async def upsert_by_idempotency_key(self, job: VideoJob) -> tuple[VideoJob, bool]: ...

    @abstractmethod
    async def save(self, job: VideoJob) -> VideoJob: ...

    @abstractmethod
    async def list_inflight(self, *, provider: str | None = None) -> list[VideoJob]: ...

    @abstractmethod
    async def find_by_provider_task_id(
        self, provider: str, provider_task_id: str
    ) -> VideoJob | None: ...


__all__ = [
    "DatabaseVideoJobRepository",
    "InMemoryVideoJobRepository",
    "StaleJobVersionError",
    "VideoJobRepository",
]
