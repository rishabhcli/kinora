"""Deterministic, no-infra tests for the render durability subsystem (§9.7, §12.1).

Covers the four hardening guarantees end-to-end with in-memory seams and a fake
render call (no provider, no DB, no ffmpeg, no spend):

* idempotency / duplicate-delivery dedup (in-flight defer + completed short-circuit),
* exactly-once accepted-clip persistence under retry,
* crash-and-resume at each non-terminal §9.7 state,
* stuck-shot recovery (resume / repair / dead-letter),
* poison crash-loop → dead-letter while still shipping a card.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.db.models.enums import ShotStatus
from app.render.checkpoint import InMemoryCheckpointStore, ShotCheckpoint
from app.render.durability import (
    Admission,
    ClipCommitter,
    DurableOutcome,
    DurableRenderGuard,
    IdempotencyGuard,
    IdempotencyKey,
    InMemoryCommitLog,
    InMemoryDeadLetterSink,
    InMemoryIdempotencyStore,
    RecoveryAction,
    ShotRecoveryService,
    StuckShot,
    spec_digest,
)
from app.render.durability.commit import AcceptedClipRecord
from app.render.poison import InMemoryPoisonStore, PoisonTracker
from app.render.states import RenderState
from app.render.steps import Step, StepLedger
from app.render.telemetry import EventKind, recording_bus

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FakeSpec:
    """A minimal ``SpecLike`` for the digest (mode + prompt + seed + refs)."""

    render_mode: Any = "i2v"
    prompt: str | None = "a quiet harbour at dawn"
    seed: int = 7
    reference_image_ids: list[str] | None = None

    def __post_init__(self) -> None:
        if self.reference_image_ids is None:
            self.reference_image_ids = ["char_a@v1", "loc_b@v2"]


def _key(shot_id: str = "shot_1", spec: FakeSpec | None = None) -> IdempotencyKey:
    return IdempotencyKey.for_spec(shot_id, spec or FakeSpec())


# --------------------------------------------------------------------------- #
# keys: spec digest stability + redesign sensitivity
# --------------------------------------------------------------------------- #


def test_spec_digest_is_stable_and_order_insensitive_on_refs() -> None:
    a = FakeSpec(reference_image_ids=["x@v1", "y@v2"])
    b = FakeSpec(reference_image_ids=["y@v2", "x@v1"])  # order swapped only
    assert spec_digest(a) == spec_digest(b)


def test_spec_digest_changes_on_redesign() -> None:
    base = FakeSpec(seed=1)
    assert spec_digest(base) != spec_digest(FakeSpec(seed=2))  # new seed = new render
    assert spec_digest(base) != spec_digest(FakeSpec(prompt="different"))


def test_idempotency_key_roundtrips() -> None:
    key = _key("shot_42")
    assert IdempotencyKey.from_str(key.as_str()) == key


# --------------------------------------------------------------------------- #
# idempotency: at-most-one live render per key
# --------------------------------------------------------------------------- #


def test_first_delivery_proceeds_duplicate_defers_while_in_flight() -> None:
    guard = IdempotencyGuard(store=InMemoryIdempotencyStore())
    key = _key()
    first = guard.begin(key)
    assert first.admission is Admission.PROCEED and first.lease is not None
    second = guard.begin(key)  # delivered again before the first finishes
    assert second.admission is Admission.IN_FLIGHT and second.lease is None


def test_completed_delivery_short_circuits_to_recorded_result() -> None:
    guard = IdempotencyGuard(store=InMemoryIdempotencyStore())
    key = _key()
    lease = guard.begin(key).lease
    assert lease is not None
    guard.complete(lease, result={"clip_key": "k/clip.mp4"})
    again = guard.begin(key)
    assert again.admission is Admission.COMPLETED
    assert again.result == {"clip_key": "k/clip.mp4"}


def test_transient_fail_releases_claim_for_retry() -> None:
    guard = IdempotencyGuard(store=InMemoryIdempotencyStore())
    key = _key()
    lease = guard.begin(key).lease
    assert lease is not None
    assert guard.fail(lease) is True
    retry = guard.begin(key)  # the next delivery may now retry
    assert retry.admission is Admission.PROCEED


def test_expired_claim_is_stealable_a_live_one_is_not() -> None:
    store = InMemoryIdempotencyStore()
    guard = IdempotencyGuard(store=store, lease_ttl_s=10.0)
    key = _key()
    held = guard.begin(key, now=1000.0)
    assert held.admission is Admission.PROCEED
    # Still live at +5s.
    assert guard.begin(key, now=1005.0).admission is Admission.IN_FLIGHT
    # Expired at +20s: a stalled holder's claim is reclaimable.
    stolen = guard.begin(key, now=1020.0)
    assert stolen.admission is Admission.PROCEED
    assert stolen.lease is not None and stolen.lease.fence != held.lease.fence  # type: ignore[union-attr]


def test_stale_holder_cannot_complete_after_being_stolen() -> None:
    store = InMemoryIdempotencyStore()
    guard = IdempotencyGuard(store=store, lease_ttl_s=10.0)
    key = _key()
    stale = guard.begin(key, now=0.0).lease
    new = guard.begin(key, now=100.0).lease  # steals the expired claim
    assert stale is not None and new is not None
    assert guard.complete(stale, result={"oops": True}) is False  # fenced out
    assert guard.complete(new, result={"ok": True}) is True


# --------------------------------------------------------------------------- #
# commit: exactly-once accepted-clip persistence under retry
# --------------------------------------------------------------------------- #


async def test_commit_runs_persist_once_then_dedups() -> None:
    log = InMemoryCommitLog()
    committer = ClipCommitter(log=log)
    key = _key()
    calls = {"n": 0}

    async def persist() -> AcceptedClipRecord:
        calls["n"] += 1
        return AcceptedClipRecord(
            key="", shot_id=key.shot_id, book_id="book_1", clip_key="k/clip.mp4", video_seconds=5.0
        )

    rec1 = await committer.commit(key, persist)
    rec2 = await committer.commit(key, persist)  # duplicate delivery / crash-resume
    assert calls["n"] == 1  # persisted exactly once
    assert rec1.clip_key == rec2.clip_key == "k/clip.mp4"
    assert log.commits == 1


async def test_commit_records_key_and_serves_recorded_video_seconds() -> None:
    committer = ClipCommitter()
    key = _key("shot_xyz")

    async def persist() -> AcceptedClipRecord:
        return AcceptedClipRecord(key="", shot_id="shot_xyz", book_id="b", video_seconds=12.5)

    rec = await committer.commit(key, persist)
    assert rec.key == key.as_str()
    existing = committer.existing(key)
    assert existing is not None and existing.video_seconds == 12.5


# --------------------------------------------------------------------------- #
# guard: the orchestrated control flow
# --------------------------------------------------------------------------- #


def _guard(**over: Any) -> DurableRenderGuard:
    defaults: dict[str, Any] = {
        "idempotency": IdempotencyGuard(store=InMemoryIdempotencyStore()),
        "checkpoints": InMemoryCheckpointStore(),
        "poison": PoisonTracker(store=InMemoryPoisonStore(), threshold=3),
        "dead_letter": InMemoryDeadLetterSink(),
    }
    defaults.update(over)
    return DurableRenderGuard(**defaults)


async def test_guard_runs_render_then_records_completion() -> None:
    guard = _guard()
    key = _key()

    async def render(ctx: Any) -> tuple[str, dict[str, Any]]:
        assert ctx.checkpoint is None  # fresh
        return "RESULT", {"clip_key": "k/clip.mp4"}

    out = await guard.run(key, "book_1", render)
    assert out.outcome is DurableOutcome.RENDERED and out.result == "RESULT"
    # A duplicate delivery now short-circuits to the recorded summary, no re-render.
    dup = await guard.run(key, "book_1", lambda _c: _fail("should not run"))
    assert dup.outcome is DurableOutcome.SKIPPED and dup.recorded == {"clip_key": "k/clip.mp4"}


async def test_guard_defers_concurrent_duplicate_delivery() -> None:
    """A second in-flight delivery must defer, not render."""
    store = InMemoryIdempotencyStore()
    guard_a = _guard(idempotency=IdempotencyGuard(store=store))
    guard_b = _guard(idempotency=IdempotencyGuard(store=store))  # another worker, shared store
    key = _key()

    # Worker A admits and is "rendering" (we don't complete it yet).
    admission = guard_a.idempotency.begin(key)
    assert admission.admission is Admission.PROCEED

    # Worker B receives the same job: it must defer.
    out_b = await guard_b.run(key, "book_1", lambda _c: _fail("B must not render"))
    assert out_b.outcome is DurableOutcome.DEFERRED


@pytest.mark.parametrize(
    "resume_state",
    [RenderState.PROMOTED, RenderState.RENDERING, RenderState.QA, RenderState.REPAIR],
)
async def test_guard_resumes_from_each_nonterminal_state(resume_state: RenderState) -> None:
    """Crash-and-resume: a mid-flight checkpoint at any state is handed to the render."""
    checkpoints = InMemoryCheckpointStore()
    key = _key()
    ledger = StepLedger(shot_id=key.shot_id)
    ledger.record(Step.RESERVE, "rk", "reservation_1")
    await checkpoints.save(
        ShotCheckpoint(
            shot_id=key.shot_id,
            book_id="book_1",
            state=resume_state,
            attempts=1,
            spent_video_seconds=5.0,
            spec_digest=key.spec_digest,
            ledger=ledger,
        )
    )
    bus, recorder = recording_bus()
    guard = _guard(checkpoints=checkpoints, bus=bus)
    seen: dict[str, Any] = {}

    async def render(ctx: Any) -> tuple[str, None]:
        seen["state"] = ctx.checkpoint.state
        seen["attempts"] = ctx.checkpoint.attempts
        seen["reserve"] = ctx.checkpoint.ledger.result_of(Step.RESERVE)
        return "OK", None

    out = await guard.run(key, "book_1", render)
    assert out.outcome is DurableOutcome.RENDERED
    assert seen["state"] is resume_state  # resumed from where it crashed
    assert seen["attempts"] == 1 and seen["reserve"] == "reservation_1"  # ledger preserved
    assert recorder.count(EventKind.RESUMED) == 1


async def test_guard_terminal_checkpoint_skips_render() -> None:
    """A render that already reached ACCEPTED never re-runs on re-delivery."""
    checkpoints = InMemoryCheckpointStore()
    key = _key()
    await checkpoints.save(
        ShotCheckpoint(
            shot_id=key.shot_id, book_id="book_1", state=RenderState.ACCEPTED,
            spec_digest=key.spec_digest,
        )
    )
    guard = _guard(checkpoints=checkpoints)
    out = await guard.run(key, "book_1", lambda _c: _fail("terminal must skip"))
    assert out.outcome is DurableOutcome.SKIPPED


async def test_guard_writes_intermediate_checkpoints() -> None:
    checkpoints = InMemoryCheckpointStore()
    bus, recorder = recording_bus()
    guard = _guard(checkpoints=checkpoints, bus=bus)
    key = _key()

    async def render(ctx: Any) -> tuple[str, None]:
        await ctx.checkpoint_state(RenderState.RENDERING, attempt=1, spent_video_seconds=5.0)
        await ctx.checkpoint_state(RenderState.QA, attempt=1)
        return "OK", None

    await guard.run(key, "book_1", render)
    snap = await checkpoints.load(key.shot_id)
    assert snap is not None and snap.state is RenderState.QA
    assert snap.revision >= 2  # two checkpoints written
    assert recorder.count(EventKind.CHECKPOINTED) == 2


# --------------------------------------------------------------------------- #
# guard: crash isolation, poison, dead-letter
# --------------------------------------------------------------------------- #


async def test_transient_crash_releases_claim_and_reraises() -> None:
    guard = _guard()
    key = _key()

    async def boom(_ctx: Any) -> tuple[Any, Any]:
        raise ConnectionError("provider blip")

    with pytest.raises(ConnectionError):
        await guard.run(key, "book_1", boom)
    # Claim released: a retry delivery is admitted (no permanent wedge on a blip).
    assert guard.idempotency.begin(key).admission is Admission.PROCEED
    assert guard.poison.failures(key.shot_id) == 1


async def test_repeated_crashes_poison_then_dead_letter() -> None:
    sink = InMemoryDeadLetterSink()
    guard = _guard(
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=3), dead_letter=sink
    )
    key = _key("crash_loop")

    async def boom(_ctx: Any) -> tuple[Any, Any]:
        raise ConnectionError("still down")

    # Transient failures accrue one each (weight 1); the 3rd crosses threshold 3.
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await guard.run(key, "book_1", boom)
        assert len(sink) == 0
    out = await guard.run(key, "book_1", boom)
    assert out.outcome is DurableOutcome.DEAD_LETTERED
    assert len(sink) == 1
    entry = sink.entries()[0]
    assert entry.shot_id == "crash_loop" and entry.failures >= 3
    # Once dead-lettered, a re-delivery short-circuits (never re-attempts the crash).
    after = await guard.run(key, "book_1", lambda _c: _fail("dead shot must not run"))
    assert after.outcome is DurableOutcome.SKIPPED
    assert after.recorded is not None and after.recorded.get("dead_lettered") is True


async def test_permanent_crash_is_not_retried() -> None:
    """A permanent failure holds the claim completed-failed (never re-renders)."""
    guard = _guard()
    key = _key()

    async def bad(_ctx: Any) -> tuple[Any, Any]:
        raise ValueError("malformed beat")  # classify_failure -> PERMANENT

    with pytest.raises(ValueError):
        await guard.run(key, "book_1", bad)
    # The next delivery does not re-render a permanently-broken shot.
    again = await guard.run(key, "book_1", lambda _c: _fail("permanent must not retry"))
    assert again.outcome is DurableOutcome.SKIPPED


# --------------------------------------------------------------------------- #
# recovery: stuck-shot scan + resume/repair/dead-letter
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FakeStuckRepo:
    """An in-memory ``StuckShotRepo`` for the recovery service."""

    shots: list[StuckShot]
    degraded: list[str]

    async def list_stuck(self, *, statuses: Any, limit: int) -> list[StuckShot]:
        keep = set(statuses)
        return [s for s in self.shots if s.status in keep][:limit]

    async def mark_degraded(self, shot_id: str) -> None:
        self.degraded.append(shot_id)


async def test_recovery_resumes_checkpointed_repairs_cold_and_deadletters_poison() -> None:
    checkpoints = InMemoryCheckpointStore()
    # 'with_ckpt' has a mid-flight checkpoint -> RESUMED; 'cold' has none -> REPAIRED.
    await checkpoints.save(
        ShotCheckpoint(shot_id="with_ckpt", book_id="b", state=RenderState.RENDERING)
    )
    poison_store = InMemoryPoisonStore()
    poison = PoisonTracker(store=poison_store, threshold=1)
    poison.record_failure("poisoned", ConnectionError("x"))  # threshold 1 -> quarantined
    assert poison.is_poisoned("poisoned")

    repo = FakeStuckRepo(
        shots=[
            StuckShot("with_ckpt", "b", ShotStatus.RENDERING),
            StuckShot("cold", "b", ShotStatus.QA),
            StuckShot("poisoned", "b", ShotStatus.RENDERING),
        ],
        degraded=[],
    )
    enqueued: list[str] = []

    async def reenqueue(shot: StuckShot) -> bool:
        enqueued.append(shot.shot_id)
        return True

    dead: list[str] = []

    async def dead_letter(*, shot_id: str, book_id: str, reason: str) -> None:
        dead.append(shot_id)

    service = ShotRecoveryService(
        repo=repo,
        reenqueue=reenqueue,
        checkpoints=checkpoints,
        poison=poison,
        dead_letter=dead_letter,
    )
    report = await service.recover_once(limit=50)

    assert report.scanned == 3
    assert report.by_action[RecoveryAction.RESUMED.value] == 1
    assert report.by_action[RecoveryAction.REPAIRED.value] == 1
    assert report.by_action[RecoveryAction.DEAD_LETTERED.value] == 1
    # The poisoned shot is degraded + dead-lettered, NOT re-enqueued.
    assert "poisoned" not in enqueued
    assert repo.degraded == ["poisoned"] and dead == ["poisoned"]
    assert set(enqueued) == {"with_ckpt", "cold"}


async def test_recovery_skips_when_enqueue_collapses_to_existing_job() -> None:
    """An idempotent enqueue that no-ops (already queued) counts as SKIPPED."""
    repo = FakeStuckRepo(shots=[StuckShot("s", "b", ShotStatus.RENDERING)], degraded=[])

    async def reenqueue(_shot: StuckShot) -> bool:
        return False  # already queued elsewhere

    service = ShotRecoveryService(repo=repo, reenqueue=reenqueue)
    report = await service.recover_once()
    assert report.by_action[RecoveryAction.SKIPPED.value] == 1
    assert report.acted == 0


async def test_recovery_tick_survives_a_scan_failure() -> None:
    @dataclass(slots=True)
    class BadRepo:
        async def list_stuck(self, *, statuses: Any, limit: int) -> list[StuckShot]:
            raise RuntimeError("db down")

        async def mark_degraded(self, shot_id: str) -> None:  # pragma: no cover
            ...

    async def reenqueue(_shot: StuckShot) -> bool:  # pragma: no cover - never reached
        return True

    service = ShotRecoveryService(repo=BadRepo(), reenqueue=reenqueue)  # type: ignore[arg-type]
    report = await service.recover_once()  # must not raise
    assert report.scanned == 0 and report.acted == 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _fail(msg: str) -> tuple[Any, Any]:
    raise AssertionError(msg)
