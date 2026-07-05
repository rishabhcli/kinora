"""RenderWorker dispatches event-granularity jobs to EventDirector instead of the
per-shot RenderPipeline, and fans the merged clip's key + each shot's own
[start, end) window back onto every original shot row (Task 9).

Shot-granularity (``job.shot_ids`` unset) must still dispatch to
``build_render_pipeline``/``RenderPipeline.render_shot`` exactly as today — the
explicit regression guard. Every collaborator ``_run_event_job`` touches (the
repos, ``EventDirector.render_event``, the shot pipeline builder) is monkeypatched
at its real import site so these tests run with no infra, mirroring the house
style of ``test_queue_worker_unit.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.db.models.enums import RenderJobStatus, RenderPriority, ShotStatus
from app.queue.redis_queue import QueuedJob
from app.queue.worker import RenderWorker
from app.render.pipeline import RenderResult

# --------------------------------------------------------------------------- #
# Infra-free doubles
# --------------------------------------------------------------------------- #


class _NullDbCtx:
    """A no-op async context manager standing in for a DB session."""

    async def __aenter__(self) -> Any:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session_factory() -> _NullDbCtx:
    return _NullDbCtx()


def _worker(**kw: Any) -> RenderWorker:
    from types import SimpleNamespace

    return RenderWorker(
        cast(Any, object()),  # queue: unused by _default_run_shot directly
        cast(Any, object()),  # redis: unused by _default_run_shot directly
        session_factory=_session_factory,
        providers=SimpleNamespace(video=object()),
        object_store=object(),
        **kw,
    )


@dataclass
class _FakeShotRow:
    """The slice of a ``shots`` row _run_event_job reads/writes."""

    id: str
    beat_id: str | None


@dataclass
class _FakeBeatRow:
    """The slice of a ``beats`` row _run_event_job reads."""

    id: str
    scene_id: str
    beat_index: int
    summary: str
    source_span: dict[str, Any]
    entities: list[str] = field(default_factory=list)
    described_visuals: str | None = None
    mood: str | None = None


def _event_job(**overrides: Any) -> QueuedJob:
    fields: dict[str, Any] = {
        "id": "job_evt_1",
        "shot_hash": "h_evt",
        "priority": RenderPriority.COMMITTED,
        "status": RenderJobStatus.QUEUED,
        "book_id": "book_1",
        "session_id": None,
        "shot_id": "s1",
        "shot_ids": ["s1", "s2", "s3"],
        "beat_id": "beat_1",
        "scene_id": "scene_1",
        "reservation_id": "res_1",
        "reserved_video_s": 15.0,
        "target_duration_s": 15.0,
    }
    fields.update(overrides)
    return QueuedJob(**fields)


def _three_shots_and_beats(
    *, degrade_second: bool = False
) -> tuple[list[_FakeShotRow], list[_FakeBeatRow]]:
    shots = [
        _FakeShotRow(id="s1", beat_id="beat_1"),
        _FakeShotRow(id="s2", beat_id="beat_2"),
        _FakeShotRow(id="s3", beat_id="beat_3"),
    ]
    # A distinct page per beat forces pack_segments to close a segment at every
    # beat (page changes always flush), so 3 beats -> 3 segments -> 3 distinct,
    # independently-offset clip windows — exactly what Step 9's assertions need.
    beats = [
        _FakeBeatRow(
            id=f"beat_{i}",
            scene_id="scene_1",
            beat_index=i - 1,
            summary=f"Beat {i} happens here with a bit more narrative detail.",
            source_span={"page": i, "para": 1, "word_range": [(i - 1) * 10, i * 10 - 1]},
        )
        for i in (1, 2, 3)
    ]
    return shots, beats


@dataclass
class _Scenario:
    result: RenderResult
    shot_updates: dict[str, dict[str, Any]]
    status_calls: list[tuple[str, ShotStatus]]
    accepted_calls: list[str]
    captured_script: Any
    shot_pipeline_calls: int
    stitch_calls: int


async def _run_event_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    job: QueuedJob | None = None,
    degraded_segment_index: int | None = None,
) -> _Scenario:
    """Wire every _run_event_job collaborator to an infra-free double, run
    ``_default_run_shot`` for one event job, and hand back everything the
    Task 9 tests need to assert on."""
    import app.render.pipeline as pipeline_module
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.shot import ShotRepo
    from app.render.event_director import EventDirector, EventRenderResult, RenderedShot
    from app.render.stitch import SceneSyncMap
    from app.render.sync_map import SyncSegment

    job = job or _event_job()
    shots, beats = _three_shots_and_beats()

    shot_updates: dict[str, dict[str, Any]] = {}
    status_calls: list[tuple[str, ShotStatus]] = []
    accepted_calls: list[str] = []
    shot_pipeline_calls = {"n": 0}
    captured: dict[str, Any] = {}

    async def fake_shot_get(self: ShotRepo, shot_id: str) -> _FakeShotRow | None:
        return next((s for s in shots if s.id == shot_id), None)

    async def fake_shot_update(self: ShotRepo, shot_id: str, **fields: Any) -> None:
        shot_updates[shot_id] = fields
        return None

    async def fake_set_status(self: ShotRepo, shot_id: str, status: ShotStatus) -> None:
        status_calls.append((shot_id, status))

    async def fake_mark_accepted(self: ShotRepo, shot_id: str, **_: Any) -> None:
        accepted_calls.append(shot_id)

    async def fake_list_by_scene(self: BeatRepo, scene_id: str) -> list[_FakeBeatRow]:
        return beats

    def fake_build_render_pipeline(*_a: Any, **_kw: Any) -> Any:
        shot_pipeline_calls["n"] += 1
        raise AssertionError("shot pipeline must not be built for an event job")

    async def fake_render_event(self: EventDirector, script: Any) -> EventRenderResult:
        captured["script"] = script
        segments = []
        rendered = []
        start = 0.0
        for i, shot in enumerate(script.shots):
            degraded = degraded_segment_index is not None and i == degraded_segment_index
            end = start + shot.duration_s
            segments.append(
                SyncSegment(
                    shot_id=shot.shot_id, video_start_s=start, video_end_s=end,
                    page=0, page_turn_at_s=start,
                )
            )
            rendered.append(
                RenderedShot(
                    shot_id=shot.shot_id, ordinal=shot.ordinal, clip_bytes=b"",
                    last_frame_bytes=None, duration_s=shot.duration_s,
                    render_mode=shot.render_mode, degraded=degraded,
                )
            )
            start = end
        sync_map = SceneSyncMap(
            scene_id=script.scene_id or script.event_id, duration_s=start, segments=segments
        )
        return EventRenderResult(
            event_id=script.event_id, scene_id=script.scene_id, book_id=script.book_id,
            clip_bytes=b"", sync_map=sync_map, duration_s=sync_map.duration_s,
            shot_count=len(rendered), rendered=rendered,
            clip_key=f"clips/{script.book_id}/{script.event_id}.mp4",
            clip_url="https://oss.test/merged.mp4",
            last_frame_keys={s.shot_id: f"lastframes/{script.book_id}/{s.shot_id}.png"
                              for s in script.shots},
        )

    monkeypatch.setattr(ShotRepo, "get", fake_shot_get)
    monkeypatch.setattr(ShotRepo, "update", fake_shot_update)
    monkeypatch.setattr(ShotRepo, "set_status", fake_set_status)
    monkeypatch.setattr(ShotRepo, "mark_accepted", fake_mark_accepted)
    monkeypatch.setattr(BeatRepo, "list_by_scene", fake_list_by_scene)
    monkeypatch.setattr(pipeline_module, "build_render_pipeline", fake_build_render_pipeline)
    monkeypatch.setattr(EventDirector, "render_event", fake_render_event)

    worker = _worker(settings=Settings(dashscope_api_key="test", render_granularity="event"))
    result = await worker._default_run_shot(job)

    return _Scenario(
        result=result,
        shot_updates=shot_updates,
        status_calls=status_calls,
        accepted_calls=accepted_calls,
        captured_script=captured.get("script"),
        shot_pipeline_calls=shot_pipeline_calls["n"],
        stitch_calls=0,
    )


# --- Step 6/7: dispatch --------------------------------------------------- #


async def test_worker_dispatches_shot_job_to_render_pipeline_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.render.pipeline as pipeline_module

    calls: dict[str, Any] = {}

    class _FakePipeline:
        async def render_shot(
            self, book_id: str, shot_id: str, *, session_id: str | None, director_present: bool
        ) -> RenderResult:
            calls["args"] = (book_id, shot_id, session_id, director_present)
            return RenderResult(shot_id=shot_id, status=ShotStatus.ACCEPTED, rung="full_video")

    def fake_build_render_pipeline(
        db: Any, *, providers: Any, object_store: Any, settings: Any
    ) -> Any:
        calls["build_called"] = True
        return _FakePipeline()

    monkeypatch.setattr(pipeline_module, "build_render_pipeline", fake_build_render_pipeline)

    worker = _worker(settings=Settings(dashscope_api_key="test"))  # default "shot" granularity
    job = QueuedJob(
        id="job_shot_1", shot_hash="h1", priority=RenderPriority.COMMITTED,
        status=RenderJobStatus.QUEUED, book_id="b1", shot_id="s1", shot_ids=None, session_id=None,
    )

    result = await worker._default_run_shot(job)

    assert calls["build_called"] is True
    assert calls["args"] == ("b1", "s1", None, False)
    assert result.status is ShotStatus.ACCEPTED


async def test_worker_dispatches_event_job_to_event_director(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _run_event_scenario(monkeypatch)

    assert scenario.captured_script is not None  # EventDirector.render_event ran
    assert scenario.shot_pipeline_calls == 0  # never the per-shot pipeline
    assert scenario.result.status is ShotStatus.ACCEPTED
    assert scenario.result.clip_key == "clips/book_1/job_evt_1.mp4"


async def test_worker_wires_budget_into_event_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (resilience audit finding): _run_event_job used to construct
    LiveEventShotRenderer with no ``budget``, so an event-granularity render
    spent real provider seconds with zero accounting against the Scheduler's
    ledger and none of the live-gate/low-buffer checks the shot-granularity
    path enforces via build_render_pipeline's own budget wiring. It must build
    one the exact same way (self._budget_factory(db)) and pass it through."""
    import app.render.live_event_renderer as live_event_renderer_module
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.shot import ShotRepo
    from app.render.event_director import EventDirector, EventRenderResult
    from app.render.stitch import SceneSyncMap

    shots, beats = _three_shots_and_beats()

    async def fake_shot_get(self: ShotRepo, shot_id: str) -> _FakeShotRow | None:
        return next((s for s in shots if s.id == shot_id), None)

    async def fake_shot_update(self: ShotRepo, shot_id: str, **fields: Any) -> None:
        return None

    async def fake_set_status(self: ShotRepo, shot_id: str, status: ShotStatus) -> None:
        return None

    async def fake_mark_accepted(self: ShotRepo, shot_id: str, **_: Any) -> None:
        return None

    async def fake_list_by_scene(self: BeatRepo, scene_id: str) -> list[_FakeBeatRow]:
        return beats

    async def fake_render_event(self: EventDirector, script: Any) -> EventRenderResult:
        return EventRenderResult(
            event_id=script.event_id,
            scene_id=script.scene_id,
            book_id=script.book_id,
            clip_bytes=b"",
            sync_map=SceneSyncMap(
                scene_id=script.scene_id or script.event_id, duration_s=0.0, segments=[]
            ),
            duration_s=0.0,
            shot_count=0,
            rendered=[],
            clip_key="clips/book_1/job_evt_1.mp4",
            clip_url="https://oss.test/merged.mp4",
            last_frame_keys={},
        )

    captured: dict[str, Any] = {}
    real_cls = live_event_renderer_module.LiveEventShotRenderer

    def capturing_ctor(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_cls(**kwargs)

    monkeypatch.setattr(ShotRepo, "get", fake_shot_get)
    monkeypatch.setattr(ShotRepo, "update", fake_shot_update)
    monkeypatch.setattr(ShotRepo, "set_status", fake_set_status)
    monkeypatch.setattr(ShotRepo, "mark_accepted", fake_mark_accepted)
    monkeypatch.setattr(BeatRepo, "list_by_scene", fake_list_by_scene)
    monkeypatch.setattr(EventDirector, "render_event", fake_render_event)
    monkeypatch.setattr(live_event_renderer_module, "LiveEventShotRenderer", capturing_ctor)

    sentinel_budget = object()
    worker = _worker(
        settings=Settings(dashscope_api_key="test", render_granularity="event"),
        budget_factory=lambda db: sentinel_budget,
    )

    await worker._default_run_shot(_event_job())

    assert captured.get("budget") is sentinel_budget


# --- Step 9: persist the merged clip's offsets onto every original shot ---- #


async def test_worker_persists_merged_clip_offsets_and_shared_clip_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _run_event_scenario(monkeypatch)

    assert set(scenario.shot_updates) == {"s1", "s2", "s3"}
    clip_keys = {sid: fields["output"]["clip_key"] for sid, fields in scenario.shot_updates.items()}
    assert len(set(clip_keys.values())) == 1  # SAME clip_key shared across the group
    assert next(iter(clip_keys.values())) == "clips/book_1/job_evt_1.mp4"

    starts = [scenario.shot_updates[sid]["clip_start_s"] for sid in ("s1", "s2", "s3")]
    ends = [scenario.shot_updates[sid]["clip_end_s"] for sid in ("s1", "s2", "s3")]
    assert starts == sorted(starts)  # correctly ordered
    assert len(set(starts)) == 3  # DIFFERENT per shot
    assert all(e > s for s, e in zip(starts, ends, strict=True))
    # None degraded in this scenario -> every shot settles as ACCEPTED.
    assert sorted(scenario.accepted_calls) == ["s1", "s2", "s3"]
    assert scenario.status_calls == []


async def test_worker_marks_only_the_degraded_segments_shot_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _run_event_scenario(monkeypatch, degraded_segment_index=1)

    assert scenario.status_calls == [("s2", ShotStatus.DEGRADED)]
    assert sorted(scenario.accepted_calls) == ["s1", "s3"]


# --- event jobs don't re-trigger the per-shot scene stitch ------------------ #


async def test_event_job_skips_per_shot_scene_stitch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An event job's shots already share ONE merged/stitched clip (Task 9); the
    per-shot SceneStitcher (one-clip-per-shot concat) must not re-run for it —
    it would concat N copies of the same event clip back into a broken scene."""
    from app.queue.fakeredis import FakeRedisClient
    from app.queue.redis_queue import RedisRenderQueue

    client = FakeRedisClient()
    queue = RedisRenderQueue(client, namespace="kinora:test:evtstitch")
    stitch_calls = {"n": 0}

    async def fake_stitch_if_complete(self: RenderWorker, scene_id: str) -> None:
        stitch_calls["n"] += 1
        return None

    monkeypatch.setattr(RenderWorker, "_stitch_scene_if_complete", fake_stitch_if_complete)

    async def run_shot(job: QueuedJob) -> RenderResult:
        return RenderResult(
            shot_id=job.shot_id or "",
            status=ShotStatus.ACCEPTED,
            rung="full_video",
            clip_key="clips/b/evt.mp4",
        )

    worker = RenderWorker(
        queue, client, run_shot=run_shot, session_factory=_session_factory, object_store=object()
    )
    await queue.enqueue(
        shot_hash="h_evt_stitch",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_evt_stitch",
        shot_id="s1",
        shot_ids=["s1", "s2", "s3"],
        scene_id="scene_1",
    )
    job = await queue.claim()
    assert job is not None and job.shot_ids == ["s1", "s2", "s3"]
    await worker.process_job(job)

    assert stitch_calls["n"] == 0  # never attempted for a grouped job
