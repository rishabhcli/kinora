"""The concrete ingest + render-shot example sagas over in-memory ports.

These assert the workflows wire the engine correctly: idempotent budget
reservation across a crash (no double-spend), reverse compensation that deletes
the artefacts a failed import / render leaves behind, and the §12.4 degrade
branch on a QA fail. No provider / DB / ffmpeg; ``KINORA_LIVE_VIDEO`` untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.sagas import (
    NO_RETRY,
    FakeClock,
    InMemoryDurableStore,
    PermanentStepError,
    RecordingBus,
    RunStatus,
    SagaEngine,
    SagaFailed,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from app.sagas.workflows.ingest import build_ingest_workflow
from app.sagas.workflows.render_shot import build_render_shot_workflow
from tests.sagas.helpers import AdvancingSleeper, Recorder, seq_run_ids


class _Crash(BaseException):
    pass


def _engine(
    wf: Workflow,
) -> tuple[SagaEngine, InMemoryDurableStore, FakeClock, RecordingBus]:
    clock = FakeClock()
    store = InMemoryDurableStore()
    bus = RecordingBus()
    engine = SagaEngine(
        WorkflowRegistry([wf]),
        store,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus,
        run_id_factory=seq_run_ids(),
    )
    return engine, store, clock, bus


# --------------------------------------------------------------------------
# Ingest pipeline
# --------------------------------------------------------------------------


class FakeIngestPort:
    """In-memory ingest side effects + an artefact tracker for assertions."""

    def __init__(self, *, fail_canon: bool = False) -> None:
        self.rec = Recorder()
        self.keyframes: dict[str, list[str]] = {}
        self.identity: dict[str, str] = {}
        self.canon: dict[str, str] = {}
        self.ready: set[str] = set()
        self.fail_canon = fail_canon

    async def parse(self, book_id: str, idempotency_key: str) -> dict[str, Any]:
        self.rec.add("parse", book_id)
        return {"pages": 10}

    async def generate_keyframes(self, book_id: str, idempotency_key: str) -> list[str]:
        self.rec.add("keyframes", book_id)
        keys = [f"kf/{book_id}/0", f"kf/{book_id}/1"]
        self.keyframes[book_id] = keys
        return keys

    async def delete_keyframes(self, book_id: str, keys: list[str]) -> None:
        self.rec.add("delete_keyframes", book_id)
        self.keyframes.pop(book_id, None)

    async def lock_identity(self, book_id: str, idempotency_key: str) -> str:
        self.rec.add("lock_identity", book_id)
        self.identity[book_id] = f"id/{book_id}"
        return self.identity[book_id]

    async def release_identity(self, book_id: str, identity_id: str) -> None:
        self.rec.add("release_identity", book_id)
        self.identity.pop(book_id, None)

    async def build_canon(self, book_id: str, idempotency_key: str) -> str:
        self.rec.add("build_canon", book_id)
        if self.fail_canon:
            raise RuntimeError("canon build failed")
        self.canon[book_id] = f"canon/{book_id}/v1"
        return self.canon[book_id]

    async def drop_canon(self, book_id: str, canon_id: str) -> None:
        self.rec.add("drop_canon", book_id)
        self.canon.pop(book_id, None)

    async def mark_ready(self, book_id: str) -> None:
        self.rec.add("mark_ready", book_id)
        self.ready.add(book_id)


async def test_ingest_happy_path() -> None:
    port = FakeIngestPort()
    wf = build_ingest_workflow(port)
    engine, store, *_ = _engine(wf)
    state = await engine.start("ingest_pipeline", {"book_id": "B1"}, run_id="I1")
    assert state.status == RunStatus.COMPLETED
    assert port.rec.ops() == ["parse", "keyframes", "lock_identity", "build_canon", "mark_ready"]
    assert "B1" in port.ready
    assert port.canon["B1"] == "canon/B1/v1"


async def test_ingest_canon_failure_unwinds_artefacts_in_reverse() -> None:
    # Make canon fail permanently so the saga compensates.
    port = FakeIngestPort(fail_canon=True)
    wf = build_ingest_workflow(port)
    # rebuild with NO_RETRY on canon so the test is fast/deterministic.
    wf = Workflow(
        name=wf.name,
        steps=tuple(s if s.name != "canon" else _no_retry(s) for s in wf.steps),
    )
    engine, store, *_ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("ingest_pipeline", {"book_id": "B2"}, run_id="I2")

    # canon failed → reverse-unwind: drop_canon (no-op), release_identity, delete_keyframes
    assert "release_identity" in port.rec.ops()
    assert "delete_keyframes" in port.rec.ops()
    # the undo ran after the forward steps, in reverse order
    undo_order = [o for o in port.rec.ops() if o.startswith(("release_", "delete_", "drop_"))]
    assert undo_order == ["release_identity", "delete_keyframes"]
    # artefacts cleaned up — no orphaned keyframes/identity/canon for B2
    assert "B2" not in port.keyframes
    assert "B2" not in port.identity
    assert "B2" not in port.canon
    assert "B2" not in port.ready


async def test_ingest_resume_does_not_redo_keyframes() -> None:
    """A crash after keyframes resumes without regenerating them (no re-spend)."""

    class _CrashKeyframeOnceAfter(FakeIngestPort):
        crashed = False

        async def lock_identity(self, book_id: str, idempotency_key: str) -> str:
            if not self.crashed:
                self.crashed = True
                raise _Crash("die after keyframes, before identity")
            return await super().lock_identity(book_id, idempotency_key)

    port = _CrashKeyframeOnceAfter()
    wf = build_ingest_workflow(port)
    engine, store, *_ = _engine(wf)
    with pytest.raises(_Crash):
        await engine.start("ingest_pipeline", {"book_id": "B3"}, run_id="I3")
    assert port.rec.count("keyframes") == 1

    final = await engine.resume("I3")
    assert final.status == RunStatus.COMPLETED
    assert port.rec.count("keyframes") == 1  # NOT regenerated on resume
    assert port.rec.count("lock_identity") == 1


async def test_ingest_requires_book_id() -> None:
    port = FakeIngestPort()
    wf = build_ingest_workflow(port)
    engine, store, *_ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("ingest_pipeline", {"no": "book"}, run_id="I4")


# --------------------------------------------------------------------------
# Render-shot saga
# --------------------------------------------------------------------------


class FakeRenderPort:
    def __init__(self, *, budget: float = 100.0, qa_pass: bool = True) -> None:
        self.rec = Recorder()
        self.remaining = budget
        self.reservations: dict[str, float] = {}
        self.objects: set[str] = set()
        self.qa_pass = qa_pass
        self._res_seq = 0

    async def reserve_budget(self, shot_id: str, seconds: float, idempotency_key: str) -> str:
        self.rec.add("reserve_budget", shot_id)
        if seconds > self.remaining:
            raise PermanentStepError("budget exhausted")
        self.remaining -= seconds
        self._res_seq += 1
        res = f"res-{self._res_seq}"
        self.reservations[res] = seconds
        return res

    async def release_budget(self, shot_id: str, reservation_id: str) -> None:
        self.rec.add("release_budget", shot_id)
        self.remaining += self.reservations.pop(reservation_id, 0.0)

    async def design(self, shot_id: str, idempotency_key: str) -> dict[str, Any]:
        self.rec.add("design", shot_id)
        return {"prompt": "a wide shot", "seed": 7}

    async def generate(self, shot_id: str, spec: dict[str, Any], idempotency_key: str) -> str:
        self.rec.add("generate", shot_id)
        return f"clip://{shot_id}"

    async def normalize(self, shot_id: str, clip: str, idempotency_key: str) -> str:
        self.rec.add("normalize", shot_id)
        return f"{clip}.norm"

    async def persist(self, shot_id: str, clip: str, idempotency_key: str) -> str:
        self.rec.add("persist", shot_id)
        oss = f"oss/{shot_id}.mp4"
        self.objects.add(oss)
        return oss

    async def delete_object(self, shot_id: str, oss_key: str) -> None:
        self.rec.add("delete_object", shot_id)
        self.objects.discard(oss_key)

    async def qa(self, shot_id: str, oss_key: str, idempotency_key: str) -> bool:
        self.rec.add("qa", shot_id)
        return self.qa_pass

    async def degrade(self, shot_id: str, idempotency_key: str) -> str:
        self.rec.add("degrade", shot_id)
        return f"oss/{shot_id}.kenburns.mp4"


async def test_render_shot_happy_path_accepts() -> None:
    port = FakeRenderPort(qa_pass=True)
    wf = build_render_shot_workflow(port)
    engine, store, *_ = _engine(wf)
    state = await engine.start("render_shot", {"shot_id": "S1", "video_seconds": 5.0}, run_id="R1")
    assert state.status == RunStatus.COMPLETED
    assert port.rec.ops() == [
        "reserve_budget",
        "design",
        "generate",
        "normalize",
        "persist",
        "qa",
    ]
    assert "degrade" not in port.rec.ops()
    assert port.remaining == 95.0  # budget spent, reservation kept on success


async def test_render_shot_qa_fail_branches_to_degrade() -> None:
    port = FakeRenderPort(qa_pass=False)
    wf = build_render_shot_workflow(port)
    engine, store, *_ = _engine(wf)
    state = await engine.start("render_shot", {"shot_id": "S2"}, run_id="R2")
    assert state.status == RunStatus.COMPLETED
    # QA failed → the §12.4 degrade rung produced a playable fallback.
    assert port.rec.ops()[-2:] == ["qa", "degrade"]


async def test_render_shot_budget_exhausted_compensates_nothing_spent() -> None:
    port = FakeRenderPort(budget=1.0)  # not enough for a 5s shot
    wf = build_render_shot_workflow(port)
    engine, store, *_ = _engine(wf)
    with pytest.raises(SagaFailed) as ei:
        await engine.start("render_shot", {"shot_id": "S3", "video_seconds": 5.0}, run_id="R3")
    assert ei.value.failed_step == "reserve_budget"
    # reservation never succeeded → budget untouched, nothing to compensate.
    assert port.remaining == 1.0
    assert port.objects == set()


async def test_render_shot_failure_after_persist_releases_budget_and_deletes_object() -> None:
    """A failure past persist must release the budget AND delete the OSS object."""

    class _FailQA(FakeRenderPort):
        async def qa(self, shot_id: str, oss_key: str, idempotency_key: str) -> bool:
            self.rec.add("qa", shot_id)
            raise RuntimeError("QA scorer crashed")  # hard failure, not a fail-score

    port = _FailQA(qa_pass=True)
    wf = build_render_shot_workflow(port)
    # NO_RETRY on qa so the hard failure compensates immediately.
    wf = Workflow(
        name=wf.name,
        steps=tuple(s if s.name != "qa" else _no_retry(s) for s in wf.steps),
    )
    engine, store, *_ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("render_shot", {"shot_id": "S4", "video_seconds": 5.0}, run_id="R4")
    # compensation released the reservation and deleted the persisted object.
    assert port.remaining == 100.0  # fully refunded
    assert port.objects == set()  # no orphaned clip
    assert "release_budget" in port.rec.ops()
    assert "delete_object" in port.rec.ops()


async def test_render_shot_resume_does_not_re_reserve_budget() -> None:
    """A crash after reserve must not double-reserve on resume."""

    class _CrashAfterReserve(FakeRenderPort):
        crashed = False

        async def design(self, shot_id: str, idempotency_key: str) -> dict[str, Any]:
            if not self.crashed:
                self.crashed = True
                raise _Crash("die after reserve, before design")
            return await super().design(shot_id, idempotency_key)

    port = _CrashAfterReserve()
    wf = build_render_shot_workflow(port)
    engine, store, *_ = _engine(wf)
    with pytest.raises(_Crash):
        await engine.start("render_shot", {"shot_id": "S5", "video_seconds": 5.0}, run_id="R5")
    assert port.rec.count("reserve_budget") == 1
    assert port.remaining == 95.0

    final = await engine.resume("R5")
    assert final.status == RunStatus.COMPLETED
    assert port.rec.count("reserve_budget") == 1  # NOT re-reserved → no double-spend
    assert port.remaining == 95.0


def _no_retry(step: Any) -> Any:
    """Return a copy of ``step`` with retries disabled (test determinism)."""
    from dataclasses import replace

    return replace(step, retry=NO_RETRY)
