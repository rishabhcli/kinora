"""Deterministic lifecycle tests for the unified async video-job engine.

No infra, no network, no real time and no spend: a :class:`ScriptedProvider`
drives the engine through every state transition, a :class:`ManualClock` makes
backoff instant + reproducible, an in-memory repo + in-memory object store make
persistence and crash-recovery observable. The webhook/poll race is forced
deterministically by gating the provider's poll on an asyncio.Event.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field

import pytest

from app.providers.resilience.backoff import JitterStrategy
from app.video.jobs import (
    AssetPersister,
    HmacWebhookVerifier,
    InMemoryVideoJobRepository,
    JobAsset,
    JobRequest,
    JobState,
    JobTransitionError,
    ManualClock,
    PersistConfig,
    PollProfile,
    ProviderPoll,
    ProviderStatus,
    ProviderSubmit,
    RecordingEventSink,
    RecordingMetricsSink,
    VideoJob,
    VideoJobEngine,
    clip_storage_key,
)
from app.video.jobs.events import JobEventType
from app.video.jobs.repository import StaleJobVersionError
from app.video.jobs.schedules import profile_for

# Fast, deterministic poll cadence (no jitter) for tests.
_TEST_PROFILE = PollProfile(
    name="test",
    base_s=1.0,
    max_interval_s=4.0,
    multiplier=2.0,
    deadline_s=100.0,
    strategy=JitterStrategy.NONE,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class ScriptedProvider:
    """A fake video provider scripted through a sequence of poll observations.

    ``submit`` hands back a task id; ``poll`` returns the next scripted
    :class:`ProviderPoll`, repeating the last once exhausted. ``poll_gate`` (if
    set) is awaited before each poll so a test can pin the loop mid-flight to
    force a webhook/poll race.
    """

    name: str = "dashscope"
    script: list[ProviderPoll] = field(default_factory=list)
    submit_status: ProviderStatus = ProviderStatus.PENDING
    submit_task_id: str = "task-1"
    submit_calls: int = 0
    poll_calls: int = 0
    cancel_calls: int = 0
    poll_raises_once: bool = False
    poll_gate: asyncio.Event | None = None

    async def submit(self, request: JobRequest, *, idempotency_key: str | None) -> ProviderSubmit:
        self.submit_calls += 1
        return ProviderSubmit(provider_task_id=self.submit_task_id, status=self.submit_status)

    async def poll(self, provider_task_id: str) -> ProviderPoll:
        if self.poll_gate is not None:
            await self.poll_gate.wait()
        if self.poll_raises_once:
            self.poll_raises_once = False
            raise RuntimeError("transient poll blip")
        self.poll_calls += 1
        idx = min(self.poll_calls - 1, len(self.script) - 1)
        return self.script[idx]

    async def cancel(self, provider_task_id: str) -> None:
        self.cancel_calls += 1

    def parse_webhook(self, payload: dict) -> ProviderPoll | None:
        status = payload.get("status")
        if status is None:
            return None
        return ProviderPoll(
            status=ProviderStatus(status),
            clip_url=payload.get("clip_url"),
            error=payload.get("error"),
        )

    def webhook_task_id(self, payload: dict) -> str | None:
        return payload.get("task_id")


@dataclass
class FakeFetcher:
    """Returns canned bytes for a clip URL (or raises to force persist failure)."""

    body: bytes = b"MP4-BYTES"
    raise_times: int = 0

    async def fetch(self, url: str) -> bytes:
        if self.raise_times > 0:
            self.raise_times -= 1
            raise RuntimeError("download blip")
        return self.body


class FakeStore:
    """In-memory object store satisfying :class:`ObjectStorePort`."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def exists(self, key: str) -> bool:
        return key in self.objects


_SECRET = "whsec-test"


def _sign(body: bytes) -> dict[str, str]:
    sig = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Kinora-Signature": sig}


def _build_engine(
    provider: ScriptedProvider,
    *,
    clock: ManualClock | None = None,
    fetcher: FakeFetcher | None = None,
    store: FakeStore | None = None,
    repo: InMemoryVideoJobRepository | None = None,
    events: RecordingEventSink | None = None,
    metrics: RecordingMetricsSink | None = None,
    persist_config: PersistConfig | None = None,
) -> tuple[VideoJobEngine, dict]:
    clock = clock or ManualClock()
    fetcher = fetcher or FakeFetcher()
    store = store or FakeStore()
    repo = repo or InMemoryVideoJobRepository()
    events = events or RecordingEventSink()
    metrics = metrics or RecordingMetricsSink()
    persister = AssetPersister(
        fetcher=fetcher,
        store=store,
        clock=clock,
        backoff=_TEST_PROFILE.schedule(),
        config=persist_config or PersistConfig(max_attempts=3),
    )
    engine = VideoJobEngine(
        provider=provider,
        repository=repo,
        asset_persister=persister,
        verifier=HmacWebhookVerifier(_SECRET),
        clock=clock,
        events=events,
        metrics=metrics,
        profile=_TEST_PROFILE,
    )
    ctx = {
        "clock": clock,
        "fetcher": fetcher,
        "store": store,
        "repo": repo,
        "events": events,
        "metrics": metrics,
    }
    return engine, ctx


# --------------------------------------------------------------------------- #
# Pure model
# --------------------------------------------------------------------------- #


def test_state_partitions_and_predicates() -> None:
    now = 0.0
    job = VideoJob(
        id="j1",
        provider="p",
        request=JobRequest(provider="p"),
        state=JobState.SUBMITTED,
        created_at=now,
        updated_at=now,
    )
    assert job.is_inflight and not job.is_terminal
    run = job.with_running(now=1.0, task_id="t")
    assert run.state is JobState.RUNNING and run.provider_task_id == "t"
    assert run.version == job.version + 1
    done = run.with_succeeded(
        JobAsset(storage_key="k", sha256="abc", size_bytes=3),
        now=2.0,
        completed_by="poll",
    )
    assert done.is_terminal and not done.is_inflight


def test_illegal_transition_raises() -> None:
    now = 0.0
    job = VideoJob(
        id="j",
        provider="p",
        request=JobRequest(provider="p"),
        state=JobState.SUCCEEDED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(JobTransitionError):
        job.with_failed("nope", now=1.0, completed_by="poll")


def test_deadline_predicate() -> None:
    job = VideoJob(
        id="j",
        provider="p",
        request=JobRequest(provider="p"),
        state=JobState.RUNNING,
        created_at=0.0,
        updated_at=0.0,
        deadline_at=10.0,
    )
    assert not job.is_expired_at(9.9)
    assert job.is_expired_at(10.0)


def test_clip_storage_key_layout() -> None:
    with_meta = VideoJob(
        id="j",
        provider="dashscope",
        request=JobRequest(provider="dashscope", metadata={"book_id": "b1", "shot_id": "s1"}),
        state=JobState.SUBMITTED,
        created_at=0.0,
        updated_at=0.0,
    )
    assert clip_storage_key(with_meta) == "clips/b1/s1.mp4"
    bare = VideoJob(
        id="jx",
        provider="minimax",
        request=JobRequest(provider="minimax"),
        state=JobState.SUBMITTED,
        created_at=0.0,
        updated_at=0.0,
    )
    assert clip_storage_key(bare) == "video-jobs/minimax/jx.mp4"


def test_profile_resolution() -> None:
    assert profile_for("dashscope").name == "dashscope"
    assert profile_for("wan2.1-t2v-turbo").name == "dashscope"
    assert profile_for("minimax").name == "minimax"
    assert profile_for("totally-unknown").name == "default"


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


async def test_repo_idempotency_upsert() -> None:
    repo = InMemoryVideoJobRepository()
    req = JobRequest(provider="p", idempotency_key="idem-1")
    j1 = VideoJob(
        id="a", provider="p", request=req, state=JobState.SUBMITTED, created_at=0, updated_at=0
    )
    stored, created = await repo.upsert_by_idempotency_key(j1)
    assert created and stored.id == "a"
    j2 = VideoJob(
        id="b", provider="p", request=req, state=JobState.SUBMITTED, created_at=0, updated_at=0
    )
    stored2, created2 = await repo.upsert_by_idempotency_key(j2)
    assert not created2 and stored2.id == "a"  # collapsed onto the first


async def test_repo_optimistic_lock() -> None:
    repo = InMemoryVideoJobRepository()
    j = VideoJob(
        id="a",
        provider="p",
        request=JobRequest(provider="p"),
        state=JobState.SUBMITTED,
        created_at=0,
        updated_at=0,
        version=0,
    )
    await repo.upsert_by_idempotency_key(j)
    v1 = j.with_running(now=1.0)  # version 1, base 0 -> ok
    await repo.save(v1)
    stale = j.with_running(now=2.0)  # also version 1, but store now holds 1
    with pytest.raises(StaleJobVersionError):
        await repo.save(stale)


# --------------------------------------------------------------------------- #
# Happy path: submit -> running -> succeeded (poll), eager persist + checksum
# --------------------------------------------------------------------------- #


async def test_full_success_via_poll() -> None:
    provider = ScriptedProvider(
        script=[
            ProviderPoll(status=ProviderStatus.RUNNING),
            ProviderPoll(status=ProviderStatus.RUNNING),
            ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/clip.mp4"),
        ]
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(
        JobRequest(provider="dashscope", duration_s=5.0, metadata={"book_id": "b", "shot_id": "s"})
    )
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.SUCCEEDED
    assert final.completed_by == "poll"
    assert final.asset is not None
    # Eager persist to object storage under the canonical clip key, with checksum.
    assert final.asset.storage_key == "clips/b/s.mp4"
    assert ctx["store"].objects["clips/b/s.mp4"] == b"MP4-BYTES"
    assert final.asset.sha256 == hashlib.sha256(b"MP4-BYTES").hexdigest()
    assert final.asset.size_bytes == len(b"MP4-BYTES")
    # Event sequence reached the terminal success + asset-persisted events.
    types = ctx["events"].types()
    assert JobEventType.SUBMITTED.value in types
    assert JobEventType.RUNNING.value in types
    assert JobEventType.ASSET_PERSISTED.value in types
    assert types[-1] == JobEventType.SUCCEEDED.value
    assert ctx["metrics"].counters["video_jobs_terminal_total"] == 1
    assert ctx["metrics"].observations.get("video_jobs_duration_s")


async def test_provider_failure_marks_failed() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.FAILED, error="content policy")]
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.FAILED
    assert final.error == "content policy"
    assert not ctx["store"].objects  # nothing persisted on failure


async def test_transient_poll_error_is_retried() -> None:
    provider = ScriptedProvider(
        poll_raises_once=True,
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")],
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.SUCCEEDED  # the blip did not kill the job


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


async def test_deadline_expiry() -> None:
    # Provider never finishes; the engine's deadline must expire it.
    provider = ScriptedProvider(script=[ProviderPoll(status=ProviderStatus.RUNNING)])
    clock = ManualClock()
    short = PollProfile(
        name="short",
        base_s=1.0,
        max_interval_s=2.0,
        multiplier=1.0,
        deadline_s=3.0,
        strategy=_TEST_PROFILE.strategy,
    )
    engine, ctx = _build_engine(provider, clock=clock)
    engine._profile = short  # tighten the deadline for the test
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.EXPIRED
    assert final.completed_by == "deadline"


async def test_success_without_url_expires_not_succeeds() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url=None)]
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.EXPIRED  # never SUCCEEDED without an asset


async def test_asset_persist_failure_expires() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")]
    )
    fetcher = FakeFetcher(raise_times=10)  # always fails -> exhausts retries
    engine, ctx = _build_engine(
        provider, fetcher=fetcher, persist_config=PersistConfig(max_attempts=2)
    )
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.EXPIRED
    assert "asset persist failed" in (final.error or "")
    assert ctx["metrics"].counters["video_jobs_asset_persist_total"] >= 1


async def test_asset_persist_retries_then_succeeds() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")]
    )
    fetcher = FakeFetcher(raise_times=1)  # one blip, then ok
    engine, ctx = _build_engine(provider, fetcher=fetcher)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.SUCCEEDED
    assert final.asset is not None


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


async def test_idempotent_submit_no_double_provider_call() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")]
    )
    engine, ctx = _build_engine(provider)
    req = JobRequest(provider="dashscope", idempotency_key="render-shot-42")
    id1 = await engine.submit(req)
    id2 = await engine.submit(req)
    assert id1 == id2
    assert provider.submit_calls == 1  # the second submit collapsed
    assert any(e.type is JobEventType.DEDUPED for e in ctx["events"].events)
    final = await engine.await_result(id1, timeout_s=5)
    assert final.state is JobState.SUCCEEDED


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


async def test_cancel_inflight_job() -> None:
    # Gate the poll so the job stays in-flight while we cancel it.
    gate = asyncio.Event()
    provider = ScriptedProvider(
        poll_gate=gate,
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")],
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    await asyncio.sleep(0)  # let the poll loop reach the gate
    cancelled = await engine.cancel(job_id, reason="user aborted")
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.error == "user aborted"
    assert provider.cancel_calls == 1
    # Releasing the gate must not flip the terminal state back.
    gate.set()
    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.CANCELLED


async def test_cancel_terminal_is_noop() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")]
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    await engine.await_result(job_id, timeout_s=5)
    again = await engine.cancel(job_id)
    assert again.state is JobState.SUCCEEDED  # unchanged
    assert provider.cancel_calls == 0  # no provider cancel on a finished job


# --------------------------------------------------------------------------- #
# Webhook completion + signature verification
# --------------------------------------------------------------------------- #


async def test_webhook_success_and_signature() -> None:
    # Gate polling so the *webhook* is the path that completes the job.
    gate = asyncio.Event()
    provider = ScriptedProvider(poll_gate=gate, submit_task_id="task-77")
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    await asyncio.sleep(0)

    body = json.dumps(
        {"task_id": "task-77", "status": "succeeded", "clip_url": "https://x/wh.mp4"}
    ).encode()
    result = await engine.handle_webhook(raw_body=body, headers=_sign(body))
    assert result.accepted and result.state is JobState.SUCCEEDED and not result.deduped

    final = await engine.await_result(job_id, timeout_s=5)
    assert final.state is JobState.SUCCEEDED
    assert final.completed_by == "webhook"
    assert ctx["store"].objects  # eager persist still happened via the webhook path
    gate.set()


async def test_webhook_bad_signature_rejected() -> None:
    provider = ScriptedProvider(poll_gate=asyncio.Event(), submit_task_id="task-9")
    engine, ctx = _build_engine(provider)
    await engine.submit(JobRequest(provider="dashscope"))
    await asyncio.sleep(0)
    body = json.dumps({"task_id": "task-9", "status": "succeeded"}).encode()
    result = await engine.handle_webhook(raw_body=body, headers={"X-Kinora-Signature": "deadbeef"})
    assert not result.accepted and result.reason == "signature verification failed"


async def test_webhook_unmatched_task() -> None:
    provider = ScriptedProvider(poll_gate=asyncio.Event(), submit_task_id="task-known")
    engine, ctx = _build_engine(provider)
    await engine.submit(JobRequest(provider="dashscope"))
    await asyncio.sleep(0)
    body = json.dumps({"task_id": "task-OTHER", "status": "succeeded"}).encode()
    result = await engine.handle_webhook(raw_body=body, headers=_sign(body))
    assert not result.accepted and result.reason == "no job for task id"
    assert any(e.type is JobEventType.WEBHOOK_UNMATCHED for e in ctx["events"].events)


# --------------------------------------------------------------------------- #
# Webhook / poll race: exactly one wins, the other reconciles
# --------------------------------------------------------------------------- #


async def test_webhook_poll_race_single_winner() -> None:
    # Poll is scripted to SUCCEED too; both paths fire near-simultaneously.
    provider = ScriptedProvider(
        submit_task_id="race-1",
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/poll.mp4")],
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))

    body = json.dumps(
        {"task_id": "race-1", "status": "succeeded", "clip_url": "https://x/wh.mp4"}
    ).encode()
    # Fire the webhook concurrently with the running poll loop.
    wh_task = asyncio.ensure_future(engine.handle_webhook(raw_body=body, headers=_sign(body)))
    final = await engine.await_result(job_id, timeout_s=5)
    wh_result = await wh_task

    assert final.state is JobState.SUCCEEDED
    # Exactly one terminal write happened (per-job lock + already-terminal guard).
    assert ctx["metrics"].counters["video_jobs_terminal_total"] == 1
    # The store has exactly one object for this clip key (no double upload).
    assert len(ctx["store"].objects) == 1
    # One side reconciled (either the webhook deduped, or the poll loop reconciled).
    reconciled = any(e.type is JobEventType.RECONCILED for e in ctx["events"].events)
    assert reconciled or wh_result.deduped


# --------------------------------------------------------------------------- #
# Crash + recover
# --------------------------------------------------------------------------- #


async def test_recover_inflight_after_crash() -> None:
    # "Worker 1" submits and persists an in-flight job, then dies before completion.
    repo = InMemoryVideoJobRepository()
    store = FakeStore()
    gate = asyncio.Event()
    provider1 = ScriptedProvider(poll_gate=gate, submit_task_id="resume-1")
    engine1, _ = _build_engine(provider1, repo=repo, store=store)
    job_id = await engine1.submit(JobRequest(provider="dashscope"))
    await asyncio.sleep(0)
    # Simulate crash: drop engine1's in-memory loop without finishing the job.
    engine1._pending[job_id].task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await engine1._pending[job_id].task
    persisted = await repo.get(job_id)
    assert persisted is not None and persisted.is_inflight  # durable, still in-flight

    # "Worker 2" boots against the SAME store and rehydrates the in-flight job.
    provider2 = ScriptedProvider(
        submit_task_id="resume-1",
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/r.mp4")],
    )
    events2 = RecordingEventSink()
    engine2, ctx2 = _build_engine(provider2, repo=repo, store=store, events=events2)
    resumed = await engine2.recover_inflight()
    assert resumed == [job_id]
    assert any(e.type is JobEventType.RECOVERED for e in events2.events)

    final = await engine2.await_result(job_id, timeout_s=5)
    assert final.state is JobState.SUCCEEDED
    assert provider2.submit_calls == 0  # recovery never re-submits
    assert store.objects  # the recovered worker persisted the asset


async def test_await_already_terminal_returns_immediately() -> None:
    provider = ScriptedProvider(
        script=[ProviderPoll(status=ProviderStatus.SUCCEEDED, clip_url="https://x/c.mp4")]
    )
    engine, ctx = _build_engine(provider)
    job_id = await engine.submit(JobRequest(provider="dashscope"))
    first = await engine.await_result(job_id, timeout_s=5)
    # Awaiting a second time on a finished job resolves instantly.
    second = await engine.await_result(job_id, timeout_s=0.5)
    assert first.state is second.state is JobState.SUCCEEDED


async def test_await_unknown_job_raises() -> None:
    provider = ScriptedProvider()
    engine, _ = _build_engine(provider)
    with pytest.raises(KeyError):
        await engine.await_result("nope")
