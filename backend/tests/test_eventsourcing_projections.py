"""Tests for the CQRS read-side: the projection manager, the reference read
models, the store-catch-up path, and the inline bus fan-out keeping read models
in lockstep with the write side."""

from __future__ import annotations

from app.db.models.enums import EntityType, SessionMode
from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.events import EventMetadata
from app.eventsourcing.domain.projection import (
    ProjectionManager,
    SessionListProjection,
    ShotStatusProjection,
)
from app.eventsourcing.domain.render_shot import (
    ShotAccepted,
    ShotPlanned,
    ShotRendered,
    ShotTransitioned,
)
from app.eventsourcing.domain.session import SessionEnded, SessionStarted
from app.eventsourcing.domain.wiring import build_command_bus
from app.eventsourcing.store.memory import InMemoryEventStore
from app.render.states import RenderState


def _meta() -> EventMetadata:
    return EventMetadata()


def test_shot_status_projection_folds_lifecycle() -> None:
    proj = ShotStatusProjection()
    proj.handle(ShotPlanned(shot_id="s1", book_id="b1", shot_hash="h"), _meta())
    proj.handle(
        ShotTransitioned(shot_id="s1", from_state="promoted", to_state="rendering"), _meta()
    )
    proj.handle(ShotRendered(shot_id="s1", clip_url="c", video_seconds=5.0), _meta())
    assert proj.shots["s1"].state == "rendering"  # last transition seen
    assert proj.shots["s1"].clip_url == "c"
    assert proj.total_video_seconds == 5.0

    proj.handle(ShotAccepted(shot_id="s1", clip_url="c"), _meta())
    assert proj.shots["s1"].accepted is True
    assert proj.shots["s1"].state == RenderState.ACCEPTED.value
    assert proj.accepted_count() == 1


def test_shot_status_cache_hit_spends_no_budget() -> None:
    proj = ShotStatusProjection()
    proj.handle(ShotPlanned(shot_id="s1", book_id="b1", shot_hash="h"), _meta())
    proj.handle(
        ShotRendered(shot_id="s1", clip_url="c", video_seconds=5.0, from_cache=True), _meta()
    )
    assert proj.total_video_seconds == 0.0  # cache hits never touch the budget


def test_shot_status_filters_by_book() -> None:
    proj = ShotStatusProjection()
    proj.handle(ShotPlanned(shot_id="a", book_id="b1", shot_hash="h"), _meta())
    proj.handle(ShotPlanned(shot_id="b", book_id="b2", shot_hash="h"), _meta())
    assert {s.shot_id for s in proj.shots_for_book("b1")} == {"a"}


def test_session_list_projection() -> None:
    proj = SessionListProjection()
    proj.handle(SessionStarted(session_id="s1", user_id="u1", book_id="b1"), _meta())
    proj.handle(SessionStarted(session_id="s2", user_id="u1", book_id="b2"), _meta())
    proj.handle(SessionEnded(session_id="s1", reason="closed"), _meta())
    assert {s.session_id for s in proj.live_sessions_for_user("u1")} == {"s2"}
    assert len(proj.sessions_for_user("u1")) == 2


def test_manager_drives_all_projections() -> None:
    shots = ShotStatusProjection()
    sessions = SessionListProjection()
    manager = ProjectionManager(projections=[shots, sessions])
    manager.apply(SessionStarted(session_id="s1", user_id="u1", book_id="b1"), _meta())
    manager.apply(ShotPlanned(shot_id="sh1", book_id="b1", shot_hash="h"), _meta())
    assert "sh1" in shots.shots
    assert "s1" in sessions.sessions


async def test_manager_catches_up_from_store() -> None:
    # Drive the write side, then replay the raw stored stream into a fresh manager.
    store = InMemoryEventStore()
    bus = build_command_bus(store)
    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b1", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    await bus.dispatch(cc.ScoreShotQA(shot_id="sh1", score=0.95, passed=True))

    shots = ShotStatusProjection()
    manager = ProjectionManager(projections=[shots])
    processed = manager.project_stored(store.all_events())
    assert processed == len(store.all_events())
    assert manager.last_position == store.all_events()[-1].global_position
    assert shots.shots["sh1"].accepted is True
    assert shots.total_video_seconds == 5.0


async def test_inline_projection_sink_keeps_read_model_in_lockstep() -> None:
    store = InMemoryEventStore()
    shots = ShotStatusProjection()
    sessions = SessionListProjection()
    manager = ProjectionManager(projections=[shots, sessions])
    bus = build_command_bus(store, projections=manager)

    await bus.dispatch(cc.StartSession(session_id="ses1", user_id="u1", book_id="b1"))
    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b1", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    await bus.dispatch(cc.ScoreShotQA(shot_id="sh1", score=0.95, passed=True))

    # Read models updated inline with every committed command — no replay needed.
    assert shots.shots["sh1"].accepted is True
    assert shots.total_video_seconds == 5.0
    assert {s.session_id for s in sessions.live_sessions_for_user("u1")} == {"ses1"}


async def test_inline_projection_reflects_canon_regen_reopen() -> None:
    """A canon edit re-opens dependent shots; the read model should show it."""
    store = InMemoryEventStore()
    shots = ShotStatusProjection()
    manager = ProjectionManager(projections=[shots])

    bus_holder: dict[str, object] = {}

    async def inline_sink(cmd: object, meta: EventMetadata) -> None:
        await bus_holder["bus"].dispatch(cmd, metadata=meta)  # type: ignore[attr-defined]

    bus = build_command_bus(store, projections=manager, saga_sink=inline_sink)
    bus_holder["bus"] = bus

    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b1", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    await bus.dispatch(cc.ScoreShotQA(shot_id="sh1", score=0.95, passed=True))
    assert shots.shots["sh1"].accepted is True

    # Edit canon -> saga re-opens sh1 -> read model flips back to PROMOTED.
    await bus.dispatch(
        cc.RegisterCanonEntity(
            entity_id="ada", book_id="b1", entity_type=EntityType.CHARACTER, name="Ada"
        )
    )
    await bus.dispatch(
        cc.EditCanonField(
            entity_id="ada", field_name="coat", new_value="red", dependent_shot_ids=("sh1",)
        )
    )
    assert shots.shots["sh1"].accepted is False
    assert shots.shots["sh1"].state == RenderState.PROMOTED.value
    assert shots.shots["sh1"].regen_count == 1


def test_switch_mode_event_is_ignored_by_shot_projection() -> None:
    # A projection ignores events it does not care about (forward-compatible fold).
    proj = ShotStatusProjection()
    from app.eventsourcing.domain.session import ModeSwitched

    proj.handle(ModeSwitched(session_id="s", mode=SessionMode.DIRECTOR.value), _meta())
    assert proj.shots == {}
