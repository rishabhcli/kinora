"""SchedulerService groups shots into events when render_granularity='event';
enqueues one job per shot (unchanged) when render_granularity='shot' (Task 9,
kinora.md §4.5 + the dormant event_director promoted live for the campaign).

Reuses ``test_scheduler_support``'s doubles exactly as ``test_scheduler_service.py``
does — ``FakeQueue.enqueue`` already accepted arbitrary ``**kw``, so it carries the
new ``shot_ids`` field through untouched; it gained one addition, ``get_job``,
backing the winning-job read-back the cross-session dedup-mismatch guard needs.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.db.models.enums import RenderPriority
from app.render.event_director import MAX_EVENT_SHOTS
from app.scheduler.model import SchedulerSession
from app.scheduler.service import SchedulerService
from tests.test_scheduler_support import (
    BOOK_ID,
    FakeBudget,
    FakeKeyframes,
    FakeQueue,
    FakeShot,
    FakeShots,
    build_shots,
)

_BASE_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240


def _settings(**overrides: object) -> Settings:
    return _BASE_SETTINGS.model_copy(update=overrides)


def _service(
    shots: list[FakeShot],
    *,
    settings: Settings,
    budget: FakeBudget | None = None,
    queue: FakeQueue | None = None,
) -> tuple[SchedulerService, FakeQueue, FakeBudget, FakeKeyframes]:
    q = queue or FakeQueue()
    b = budget or FakeBudget()
    k = FakeKeyframes()
    svc = SchedulerService(
        queue=q, budget=b, shots=FakeShots(shots), keyframes=k, settings=settings
    )
    return svc, q, b, k


def _session(**kw: object) -> SchedulerSession:
    return SchedulerSession(session_id="sess_evt", book_id=BOOK_ID, **kw)


# --- the regression guard: shot-granularity (default) is untouched ---------- #


async def test_shot_granularity_unchanged_by_default() -> None:
    shots = build_shots(8, spacing=10, duration_s=5.0)  # one scene, 8 shots
    svc, queue, budget, _ = _service(shots, settings=_settings(render_granularity="shot"))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    committed = queue.by_priority(RenderPriority.COMMITTED)
    assert committed  # something got promoted
    assert len(committed) == len(promoted)  # one job per promoted shot, exactly today
    assert all(job.get("shot_ids") is None for job in committed)
    assert all(job["shot_id"] is not None for job in committed)
    # One reservation per shot too (today's exact granularity of spend).
    assert len(budget.reserves) == len(promoted)


# --- event granularity: grouping ------------------------------------------- #


async def test_event_granularity_groups_shots_into_packed_segments() -> None:
    shots = build_shots(8, spacing=10, duration_s=5.0)  # one scene, 8 shots
    svc, queue, _, _ = _service(shots, settings=_settings(render_granularity="event"))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    committed = queue.by_priority(RenderPriority.COMMITTED)
    assert committed
    assert len(committed) < len(promoted)  # fewer jobs than shots — they were grouped
    assert all(job.get("shot_ids") for job in committed)  # every job carries a group

    all_grouped_ids = [sid for job in committed for sid in job["shot_ids"]]
    assert sorted(all_grouped_ids) == sorted(promoted)  # every promoted shot lands in a group
    assert all(len(job["shot_ids"]) <= MAX_EVENT_SHOTS for job in committed)


async def test_event_granularity_caps_batch_at_max_event_shots() -> None:
    # More ready shots in one scene than MAX_EVENT_SHOTS (6): must split into >1 job.
    shots = build_shots(10, spacing=10, duration_s=5.0)
    svc, queue, _, _ = _service(shots, settings=_settings(render_granularity="event"))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    committed = queue.by_priority(RenderPriority.COMMITTED)
    assert len(promoted) == 10
    assert len(committed) == 2  # 6 + 4
    assert len(committed[0]["shot_ids"]) == MAX_EVENT_SHOTS
    assert len(committed[1]["shot_ids"]) == 4


async def test_event_granularity_stops_batch_at_scene_boundary() -> None:
    scene_a = [
        FakeShot(id=f"a_{i}", beat_id=f"beat_a_{i}", scene_id="scene_A", word_index_start=i * 10)
        for i in range(1, 4)  # words 10, 20, 30
    ]
    scene_b = [
        FakeShot(
            id=f"b_{i}", beat_id=f"beat_b_{i}", scene_id="scene_B", word_index_start=30 + i * 10
        )
        for i in range(1, 4)  # words 40, 50, 60
    ]
    svc, queue, _, _ = _service(scene_a + scene_b, settings=_settings(render_granularity="event"))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    committed = queue.by_priority(RenderPriority.COMMITTED)
    assert len(promoted) == 6
    assert len(committed) == 2  # one job per scene, never spanning the boundary
    assert all(sid.startswith("a_") for sid in committed[0]["shot_ids"])
    assert all(sid.startswith("b_") for sid in committed[1]["shot_ids"])
    assert committed[0]["scene_id"] == "scene_A"
    assert committed[1]["scene_id"] == "scene_B"


async def test_event_granularity_reserves_once_per_batch_not_per_shot() -> None:
    # All 6 shots fit in exactly one MAX_EVENT_SHOTS batch in one scene.
    shots = build_shots(6, spacing=10, duration_s=5.0)
    svc, queue, budget, _ = _service(shots, settings=_settings(render_granularity="event"))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    committed = queue.by_priority(RenderPriority.COMMITTED)
    assert len(promoted) == 6
    assert len(committed) == 1
    # ONE aggregate reservation for the whole batch, not one per shot: the
    # worker's pre-existing _release_earmark releases exactly job.reservation_id
    # before rendering (unconditionally, for every job) — splitting this into 6
    # separate reservations would leak 5 of them (never released/committed).
    assert len(budget.reserves) == 1
    assert budget.reserves[0] == sum(s.duration_s for s in shots)
    assert committed[0]["reserved_video_s"] == sum(s.duration_s for s in shots)


async def test_event_granularity_dedup_never_drops_shots_the_winning_job_omits() -> None:
    """The batch's dedup key identifies only its FIRST shot (Step 1's
    backward-compatible convention), so a differently-sized batch enqueued by
    another tick/session can win the idempotency race for the same first shot
    (their readiness gates can diverge — same first shot, different
    focus_word/velocity => a different eta/budget cutoff). A dedup hit must
    never make this tick optimistically treat its WHOLE local batch as
    handled: only the shots the winning job actually lists get marked
    buffered, so any it omits are left for a later tick's
    next_uncommitted_shot to naturally re-offer instead of silently losing
    them.
    """
    shots = build_shots(3, spacing=10, duration_s=5.0)  # one scene, 3 shots
    queue = FakeQueue()
    # Simulate another tick/session having already won the dedup race with a
    # SMALLER batch — covering only the first shot, not the other two.
    await queue.enqueue(
        shot_hash=f"shot:{BOOK_ID}:{shots[0].id}",
        priority=RenderPriority.COMMITTED,
        book_id=BOOK_ID,
        job_id="winning_job",
        shot_id=shots[0].id,
        shot_ids=[shots[0].id],
    )
    svc, _, _, _ = _service(shots, settings=_settings(render_granularity="event"), queue=queue)
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    promoted = await svc._fill_committed(session)

    assert promoted == []  # deduped, not a fresh promotion
    buffered = {b.shot_id for b in session.committed_buffer}
    assert buffered == {shots[0].id}  # only what the winning job actually covers
    assert shots[1].id not in buffered  # left alone -> re-offered next tick
    assert shots[2].id not in buffered
