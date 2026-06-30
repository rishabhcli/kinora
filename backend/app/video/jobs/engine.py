"""The :class:`VideoJobEngine` — the unified async video-job lifecycle.

One engine drives every async hosted-video render, regardless of provider, with a
single clean API:

* ``await submit(request) -> job_id`` — idempotent on the request's idempotency
  key; submits to the provider, persists the durable job, and starts polling.
* ``await await_result(job_id) -> VideoJob`` — resolves when the job reaches a
  terminal state, via *whichever* path got there first (poll loop, webhook, or
  the deadline sweeper).
* ``await handle_webhook(raw_body, headers) -> WebhookResult`` — verifies the
  signature, matches the task to a job, and terminalizes it (reconciling against
  a concurrent poll so neither double-processes).
* ``await recover_inflight() -> list[str]`` — rehydrates in-flight jobs from the
  store after a worker crash and restarts their poll loops.
* ``await cancel(job_id)`` — best-effort provider cancel + terminal transition.

Determinism: the engine reads time and sleeps only through the injected
:class:`~app.video.jobs.ports.JobClock`, persists only through a
:class:`~app.video.jobs.repository.VideoJobRepository`, and talks to exactly one
provider adapter, object store, and pair of observability sinks — all protocols.
That is what lets the tests script every transition, a crash/recover, a
webhook/poll race, and expiry with a fake clock and an in-memory store.

The webhook/poll race is settled in one place — :meth:`_terminalize` — which
re-reads the job under the engine's per-job lock, no-ops if it is already
terminal, and persists under optimistic-concurrency. The loser of the race emits
a ``RECONCILED`` event and changes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import structlog

from app.db.base import new_id

from .assets import AssetPersister
from .clock import SystemClock
from .events import JobEvent, JobEventType
from .models import JobRequest, JobState, VideoJob
from .observability import NullMetricsSink, StructlogEventSink
from .ports import (
    EventSink,
    JobClock,
    MetricsSink,
    ProviderPoll,
    ProviderStatus,
    VideoJobProvider,
    WebhookResult,
    WebhookVerifier,
)
from .repository import StaleJobVersionError, VideoJobRepository
from .schedules import PollProfile, profile_for
from .webhook import clip_storage_key

_log = structlog.get_logger("video.jobs.engine")

#: Maps a provider's normalized status to the terminal job state it implies.
_TERMINAL_FOR_STATUS = {
    ProviderStatus.FAILED: JobState.FAILED,
    ProviderStatus.EXPIRED: JobState.EXPIRED,
}


@dataclass(frozen=True, slots=True)
class _Pending:
    """Bookkeeping for an in-flight poll loop + its awaiters."""

    done: asyncio.Event
    task: asyncio.Task[None]


class VideoJobEngine:
    """Provider-agnostic async video-job lifecycle orchestrator."""

    def __init__(
        self,
        *,
        provider: VideoJobProvider,
        repository: VideoJobRepository,
        asset_persister: AssetPersister,
        verifier: WebhookVerifier,
        clock: JobClock | None = None,
        events: EventSink | None = None,
        metrics: MetricsSink | None = None,
        profile: PollProfile | None = None,
    ) -> None:
        self._provider = provider
        self._repo = repository
        self._assets = asset_persister
        self._verifier = verifier
        self._clock: JobClock = clock or SystemClock()
        self._events: EventSink = events or StructlogEventSink()
        self._metrics: MetricsSink = metrics or NullMetricsSink()
        self._profile = profile or profile_for(provider.name)
        #: Per-job coordination: a completion Event + the running poll task.
        self._pending: dict[str, _Pending] = {}
        #: Per-job lock so poll vs webhook vs cancel serialize on terminalization.
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def submit(self, request: JobRequest) -> str:
        """Submit a render; return its job id. Idempotent on the idempotency key.

        A second ``submit`` with the same ``(provider, idempotency_key)`` returns
        the original job id and submits nothing to the provider — so a retried
        caller, or a duplicated message, never spends twice.
        """
        if request.idempotency_key is not None:
            existing = await self._repo.get_by_idempotency_key(
                self._provider.name, request.idempotency_key
            )
            if existing is not None:
                self._emit(JobEventType.DEDUPED, existing)
                self._metrics.incr(
                    "video_jobs_submitted_total", provider=self._provider.name, result="deduped"
                )
                self._ensure_poll_loop(existing)
                return existing.id

        now = self._clock.now()
        job = VideoJob(
            id=new_id(),
            provider=self._provider.name,
            request=request,
            state=JobState.SUBMITTED,
            created_at=now,
            updated_at=now,
            deadline_at=now + self._profile.deadline_s,
        )
        stored, created = await self._repo.upsert_by_idempotency_key(job)
        if not created:
            # Lost a concurrent submit race; adopt the winner.
            self._emit(JobEventType.DEDUPED, stored)
            self._metrics.incr(
                "video_jobs_submitted_total", provider=self._provider.name, result="deduped"
            )
            self._ensure_poll_loop(stored)
            return stored.id

        submitted = await self._provider.submit(request, idempotency_key=request.idempotency_key)
        stored = stored.with_provider_task_id(submitted.provider_task_id, now=self._clock.now())
        if submitted.status is ProviderStatus.RUNNING:
            stored = stored.with_running(now=self._clock.now())
        stored = await self._save(stored)
        self._emit(JobEventType.SUBMITTED, stored, provider_task_id=stored.provider_task_id)
        self._metrics.incr(
            "video_jobs_submitted_total", provider=self._provider.name, result="created"
        )
        self._ensure_poll_loop(stored)
        return stored.id

    async def await_result(self, job_id: str, *, timeout_s: float | None = None) -> VideoJob:
        """Wait until ``job_id`` is terminal and return its final snapshot.

        Resolves on whatever path terminalizes the job (poll, webhook, deadline,
        cancel). Returns immediately if the job is already terminal. ``timeout_s``
        bounds the *wait*, not the job's own deadline (it raises
        :class:`asyncio.TimeoutError` if exceeded — the job keeps running).
        """
        job = await self._repo.get(job_id)
        if job is None:
            raise KeyError(f"unknown video job {job_id}")
        if job.is_terminal:
            return job
        pending = self._pending.get(job_id)
        if pending is None:
            # No live loop (e.g. resumed elsewhere); start one so we get woken.
            self._ensure_poll_loop(job)
            pending = self._pending[job_id]
        if timeout_s is None:
            await pending.done.wait()
        else:
            await asyncio.wait_for(pending.done.wait(), timeout=timeout_s)
        final = await self._repo.get(job_id)
        assert final is not None  # noqa: S101 - job rows are never deleted
        return final

    async def handle_webhook(self, *, raw_body: bytes, headers: dict[str, str]) -> WebhookResult:
        """Ingest a provider webhook: verify, match, terminalize (reconciling).

        A failed signature or an unmatched/unknown task never mutates a job. A
        matched completion is applied through the same :meth:`_terminalize` path
        the poller uses, so a webhook and a poll cannot both finish the job.
        """
        if not self._verifier.verify(raw_body=raw_body, headers=headers):
            self._metrics.incr(
                "video_jobs_webhook_total", provider=self._provider.name, result="unverified"
            )
            return WebhookResult(accepted=False, reason="signature verification failed")

        payload = self._parse_json(raw_body)
        if payload is None:
            return WebhookResult(accepted=False, reason="unparseable webhook body")

        task_id = self._provider.webhook_task_id(payload)
        observation = self._provider.parse_webhook(payload)
        if task_id is None or observation is None:
            self._emit_bare(JobEventType.WEBHOOK_UNMATCHED, task_id=task_id)
            self._metrics.incr(
                "video_jobs_webhook_total", provider=self._provider.name, result="unmatched"
            )
            return WebhookResult(accepted=False, reason="webhook not recognized")

        job = await self._repo.find_by_provider_task_id(self._provider.name, task_id)
        if job is None:
            self._emit_bare(JobEventType.WEBHOOK_UNMATCHED, task_id=task_id)
            self._metrics.incr(
                "video_jobs_webhook_total", provider=self._provider.name, result="unmatched"
            )
            return WebhookResult(accepted=False, reason="no job for task id")

        self._emit(JobEventType.WEBHOOK_RECEIVED, job, status=observation.status.value)
        self._metrics.incr(
            "video_jobs_webhook_total", provider=self._provider.name, result="matched"
        )
        final = await self._apply_observation(job.id, observation, source="webhook")
        deduped = final.completed_by is not None and final.completed_by != "webhook"
        return WebhookResult(
            accepted=True,
            job_id=final.id,
            state=final.state,
            deduped=deduped,
        )

    async def recover_inflight(self) -> list[str]:
        """Rehydrate in-flight jobs from the store and restart their poll loops.

        Called on worker boot. A job persisted as ``SUBMITTED``/``RUNNING`` before a
        crash is resumed exactly where it was — its ``provider_task_id`` and
        ``deadline_at`` came off the durable row, so polling continues and a clip
        whose URL has since expired is correctly observed as EXPIRED rather than
        lost. Already-terminal jobs are skipped.
        """
        jobs = await self._repo.list_inflight(provider=self._provider.name)
        resumed: list[str] = []
        for job in jobs:
            if job.id in self._pending:
                continue
            self._emit(JobEventType.RECOVERED, job)
            self._metrics.incr("video_jobs_recovered_total", provider=self._provider.name)
            self._ensure_poll_loop(job)
            resumed.append(job.id)
        return resumed

    async def cancel(self, job_id: str, *, reason: str | None = None) -> VideoJob:
        """Cancel a job: best-effort provider cancel + a terminal CANCELLED state.

        A no-op if the job is already terminal (returns the existing snapshot).
        The provider cancel is best-effort: its failure never blocks marking the
        job cancelled locally.
        """
        lock = self._lock_for(job_id)
        async with lock:
            job = await self._repo.get(job_id)
            if job is None:
                raise KeyError(f"unknown video job {job_id}")
            if job.is_terminal:
                return job
            if job.provider_task_id is not None:
                with contextlib.suppress(Exception):
                    await self._provider.cancel(job.provider_task_id)
            job = await self._save(job.with_cancelled(now=self._clock.now(), reason=reason))
        self._emit(JobEventType.CANCELLED, job)
        self._metrics.incr(
            "video_jobs_terminal_total",
            provider=self._provider.name,
            state=JobState.CANCELLED.value,
        )
        self._signal_done(job_id)
        return job

    # ------------------------------------------------------------------ #
    # Poll loop
    # ------------------------------------------------------------------ #

    def _ensure_poll_loop(self, job: VideoJob) -> None:
        """Start (once) the background poll loop for an in-flight job."""
        if job.is_terminal or job.id in self._pending:
            if job.is_terminal:
                # Make a stray awaiter resolvable.
                self._pending.setdefault(job.id, _Pending(done=_set_event(), task=_noop_task()))
            return
        done = asyncio.Event()
        task = asyncio.ensure_future(self._poll_loop(job.id, done))
        self._pending[job.id] = _Pending(done=done, task=task)

    async def _poll_loop(self, job_id: str, done: asyncio.Event) -> None:
        """Poll the provider with backoff until the job is terminal or deadlined."""
        schedule = self._profile.schedule()
        attempt = 0
        try:
            while True:
                job = await self._repo.get(job_id)
                if job is None or job.is_terminal:
                    return
                now = self._clock.now()
                if job.is_expired_at(now):
                    await self._apply_observation(
                        job_id,
                        ProviderPoll(status=ProviderStatus.EXPIRED, error="deadline elapsed"),
                        source="deadline",
                    )
                    return
                if job.provider_task_id is None:
                    # Submit hasn't stamped the task id yet; wait a beat.
                    await self._clock.sleep(self._profile.base_s)
                    continue

                attempt += 1
                observation = await self._safe_poll(job.provider_task_id)
                job = await self._save(job.with_polled(now=self._clock.now()))
                self._emit(
                    JobEventType.POLLED, job, attempt=attempt, status=observation.status.value
                )
                self._metrics.incr("video_jobs_poll_total", provider=self._provider.name)

                if observation.status is ProviderStatus.RUNNING and job.state is JobState.SUBMITTED:
                    job = await self._save(job.with_running(now=self._clock.now()))
                    self._emit(JobEventType.RUNNING, job)

                if observation.status is not ProviderStatus.PENDING and (
                    observation.status is not ProviderStatus.RUNNING
                ):
                    await self._apply_observation(job_id, observation, source="poll")
                    return

                delay = schedule.next_delay(attempt, retry_after_s=observation.retry_after_s)
                await self._clock.sleep(delay)
        finally:
            done.set()

    async def _safe_poll(self, provider_task_id: str) -> ProviderPoll:
        """Poll, mapping a transport error to a transient PENDING (keep polling)."""
        try:
            return await self._provider.poll(provider_task_id)
        except Exception as exc:  # noqa: BLE001 - transient; deadline still bounds us
            _log.warning("video_job_poll_error", task_id=provider_task_id, error=str(exc))
            return ProviderPoll(status=ProviderStatus.PENDING, error=str(exc))

    # ------------------------------------------------------------------ #
    # Terminalization (the single reconciliation point)
    # ------------------------------------------------------------------ #

    async def _apply_observation(
        self, job_id: str, observation: ProviderPoll, *, source: str
    ) -> VideoJob:
        """Apply a terminal observation under the per-job lock, reconciling races."""
        lock = self._lock_for(job_id)
        async with lock:
            job = await self._repo.get(job_id)
            if job is None:
                raise KeyError(f"unknown video job {job_id}")
            if job.is_terminal:
                # Another path (poll/webhook) already finished it: reconcile, no-op.
                self._emit(JobEventType.RECONCILED, job, source=source, final_state=job.state.value)
                self._metrics.incr("video_jobs_reconciled_total", provider=self._provider.name)
                self._signal_done(job_id)
                return job

            if observation.status is ProviderStatus.SUCCEEDED:
                job = await self._succeed(job, observation, source=source)
            elif observation.status in _TERMINAL_FOR_STATUS:
                target = _TERMINAL_FOR_STATUS[observation.status]
                if target is JobState.EXPIRED:
                    job = job.with_expired(
                        now=self._clock.now(), completed_by=source, error=observation.error
                    )
                else:
                    job = job.with_failed(
                        observation.error or "provider reported failure",
                        now=self._clock.now(),
                        completed_by=source,
                    )
                job = await self._save(job)
                self._emit_terminal(job, source=source)
            else:
                # Non-terminal observation arriving via this path: just return.
                return job

        self._signal_done(job_id)
        return job

    async def _succeed(self, job: VideoJob, observation: ProviderPoll, *, source: str) -> VideoJob:
        """Eagerly persist the (expiring) clip, then mark the job SUCCEEDED.

        If persistence fails the job becomes EXPIRED (the URL is gone / unreachable),
        never SUCCEEDED-without-an-asset — a downstream reader can always trust a
        SUCCEEDED job has durable bytes.
        """
        if not observation.clip_url:
            failed = await self._save(
                job.with_expired(
                    now=self._clock.now(),
                    completed_by=source,
                    error="provider succeeded without a clip url",
                )
            )
            self._emit_terminal(failed, source=source)
            return failed

        job = await self._save(job.with_download_attempt(now=self._clock.now()))
        key = clip_storage_key(job)
        try:
            asset = await self._assets.persist(url=observation.clip_url, storage_key=key)
        except Exception as exc:  # noqa: BLE001 - URL expired / unreachable
            expired = await self._save(
                job.with_expired(
                    now=self._clock.now(),
                    completed_by=source,
                    error=f"asset persist failed: {exc}",
                )
            )
            self._emit_terminal(expired, source=source)
            self._metrics.incr(
                "video_jobs_asset_persist_total", provider=self._provider.name, result="failed"
            )
            return expired

        self._emit(
            JobEventType.ASSET_PERSISTED,
            job,
            storage_key=asset.storage_key,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
        )
        self._metrics.incr(
            "video_jobs_asset_persist_total", provider=self._provider.name, result="ok"
        )
        succeeded = await self._save(
            job.with_succeeded(asset, now=self._clock.now(), completed_by=source)
        )
        self._emit_terminal(succeeded, source=source)
        elapsed = succeeded.updated_at - succeeded.created_at
        self._metrics.observe("video_jobs_duration_s", elapsed, provider=self._provider.name)
        return succeeded

    # ------------------------------------------------------------------ #
    # Persistence + signalling helpers
    # ------------------------------------------------------------------ #

    async def _save(self, job: VideoJob) -> VideoJob:
        """Persist a snapshot, retrying once on a stale-version conflict by reload.

        Under the per-job lock the engine is the only writer, so a conflict here
        means another process touched the row (multi-worker). We reload and let
        the caller's terminal/no-op logic re-evaluate on the next pass rather than
        clobber a concurrent terminalization.
        """
        try:
            return await self._repo.save(job)
        except StaleJobVersionError:
            current = await self._repo.get(job.id)
            if current is not None:
                return current
            raise

    def _lock_for(self, job_id: str) -> asyncio.Lock:
        lock = self._locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[job_id] = lock
        return lock

    def _signal_done(self, job_id: str) -> None:
        pending = self._pending.get(job_id)
        if pending is not None:
            pending.done.set()

    # ------------------------------------------------------------------ #
    # Event/metric emission
    # ------------------------------------------------------------------ #

    def _emit(
        self, type_: JobEventType, job: VideoJob, *, source: str | None = None, **detail: object
    ) -> None:
        self._events.emit(
            JobEvent.from_job(type_, job, at=self._clock.now(), source=source, **detail)
        )

    def _emit_bare(self, type_: JobEventType, **detail: object) -> None:
        self._events.emit(
            JobEvent(
                type=type_,
                job_id="-",
                provider=self._provider.name,
                at=self._clock.now(),
                detail=dict(detail),
            )
        )

    def _emit_terminal(self, job: VideoJob, *, source: str) -> None:
        mapping = {
            JobState.SUCCEEDED: JobEventType.SUCCEEDED,
            JobState.FAILED: JobEventType.FAILED,
            JobState.EXPIRED: JobEventType.EXPIRED,
            JobState.CANCELLED: JobEventType.CANCELLED,
        }
        self._emit(mapping[job.state], job, source=source)
        self._metrics.incr(
            "video_jobs_terminal_total", provider=self._provider.name, state=job.state.value
        )

    @staticmethod
    def _parse_json(raw_body: bytes) -> dict[str, object] | None:
        import json

        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None


def _set_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


def _noop_task() -> asyncio.Task[None]:
    async def _noop() -> None:
        return None

    return asyncio.ensure_future(_noop())


__all__ = ["VideoJobEngine"]
