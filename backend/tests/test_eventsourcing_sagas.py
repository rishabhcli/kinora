"""Tests for the saga-trigger seam + the concrete §9.7/§5.4 triggers, including
the inline-sink end-to-end path (a canon edit surgically regenerating the
dependent shots through the same bus)."""

from __future__ import annotations

from app.db.models.enums import EntityType, SessionMode
from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.canon import CanonFieldEdited
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.events import EventMetadata
from app.eventsourcing.domain.render_shot import RenderShotAggregate, RenderState
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.saga import SagaDispatcher
from app.eventsourcing.domain.sagas_catalog import (
    on_canon_edit,
    on_director_comment,
    register_default_sagas,
)
from app.eventsourcing.domain.session import DirectorCommentLeft
from app.eventsourcing.domain.wiring import build_command_bus
from app.eventsourcing.store.memory import InMemoryEventStore


def test_director_comment_trigger_targets_the_shot() -> None:
    event = DirectorCommentLeft(
        session_id="s1", comment_id="c1", shot_id="shot7", note="too fast", routed_agent="cine"
    )
    commands = on_director_comment(event, EventMetadata())
    assert len(commands) == 1
    assert isinstance(commands[0], cc.RegenerateShot)
    assert commands[0].shot_id == "shot7"
    assert commands[0].reason == "too fast"


def test_canon_edit_trigger_fans_out_per_dependent_shot() -> None:
    event = CanonFieldEdited(
        entity_id="e1",
        field_name="coat",
        new_value="red",
        canon_version=2,
        dependent_shot_ids=("shotA", "shotB", "shotC"),
    )
    commands = on_canon_edit(event, EventMetadata())
    assert all(isinstance(c, cc.RegenerateShot) for c in commands)
    assert [c.shot_id for c in commands] == ["shotA", "shotB", "shotC"]  # type: ignore[attr-defined]


def test_canon_edit_no_dependents_triggers_nothing() -> None:
    event = CanonFieldEdited(entity_id="e1", field_name="x", new_value="y", canon_version=2)
    assert on_canon_edit(event, EventMetadata()) == ()


async def test_dispatcher_carries_causation_chain() -> None:
    dispatcher = SagaDispatcher()
    dispatcher.register(
        DirectorCommentLeft.event_type,
        lambda e, m: (cc.RegenerateShot(shot_id="shot1"),),
    )
    source = EventMetadata(event_id="origin", correlation_id="corr")
    triggered = await dispatcher.dispatch(
        [
            DirectorCommentLeft(
                session_id="s", comment_id="c", shot_id="shot1", note="n", routed_agent="a"
            )
        ],
        source_metadata=source,
    )
    assert len(triggered) == 1
    assert triggered[0].metadata.correlation_id == "corr"
    assert triggered[0].metadata.causation_id == "origin"


async def test_dispatcher_forwards_to_sink() -> None:
    sunk: list[Command] = []

    async def sink(cmd: Command, _meta: EventMetadata) -> None:
        sunk.append(cmd)

    dispatcher = SagaDispatcher(sink=sink)
    register_default_sagas(dispatcher)
    await dispatcher.dispatch(
        [
            CanonFieldEdited(
                entity_id="e1",
                field_name="coat",
                new_value="red",
                canon_version=2,
                dependent_shot_ids=("shotA",),
            )
        ],
        source_metadata=EventMetadata(event_id="o"),
    )
    assert len(sunk) == 1
    assert isinstance(sunk[0], cc.RegenerateShot)


async def test_canon_edit_surgically_regenerates_dependent_shots_end_to_end() -> None:
    """§5.4: editing a canon field regenerates *only* the dependent shots.

    Wire the saga sink to re-dispatch on the same bus, so a single
    ``EditCanonField`` command re-opens the dependent shots back to Promoted while
    leaving every other shot untouched.
    """
    store = InMemoryEventStore()

    # The sink re-dispatches triggered commands inline on the same bus.
    bus_holder: dict[str, object] = {}

    async def inline_sink(cmd: Command, meta: EventMetadata) -> None:
        await bus_holder["bus"].dispatch(cmd, metadata=meta)  # type: ignore[attr-defined]

    bus = build_command_bus(store, saga_sink=inline_sink)
    bus_holder["bus"] = bus

    # Two shots driven all the way to Accepted (a settled, cached clip).
    shot_repo: Repository[RenderShotAggregate] = Repository(store, RenderShotAggregate)
    for shot_id in ("shotA", "shotB"):
        await bus.dispatch(
            cc.PlanShot(
                shot_id=shot_id, book_id="b", scene_id="s", beat_id="be", shot_hash=f"h-{shot_id}"
            )
        )
        await bus.dispatch(cc.PromoteShot(shot_id=shot_id))
        await bus.dispatch(
            cc.RenderShot(
                shot_id=shot_id,
                shot_hash=f"h-{shot_id}",
                cache_hit=False,
                clip_url="c",
                video_seconds=5.0,
            )
        )
        await bus.dispatch(cc.ScoreShotQA(shot_id=shot_id, score=0.95, passed=True))

    # A third, also-Accepted shot that does NOT depend on the entity — untouched.
    await bus.dispatch(
        cc.PlanShot(shot_id="shotC", book_id="b", scene_id="s", beat_id="be", shot_hash="h-c")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="shotC"))
    await bus.dispatch(
        cc.RenderShot(shot_id="shotC", shot_hash="h-c", cache_hit=True, clip_url="cached")
    )

    # Register + edit the canon entity, declaring shotA & shotB as dependents.
    await bus.dispatch(
        cc.RegisterCanonEntity(
            entity_id="ada", book_id="b", entity_type=EntityType.CHARACTER, name="Ada"
        )
    )
    await bus.dispatch(
        cc.EditCanonField(
            entity_id="ada",
            field_name="coat_color",
            new_value="red",
            dependent_shot_ids=("shotA", "shotB"),
        )
    )

    # The saga re-opened exactly the dependent shots: shotA/shotB went
    # Accepted -> Promoted (a fresh render attempt); shotC stays Accepted.
    a = await shot_repo.load("shotA")
    b = await shot_repo.load("shotB")
    c = await shot_repo.load("shotC")
    assert a.state is RenderState.PROMOTED
    assert b.state is RenderState.PROMOTED
    assert a.repair_count == 0  # fresh attempt budget
    assert c.state is RenderState.ACCEPTED  # untouched — surgical, not a full re-render


async def test_director_comment_regen_through_bus() -> None:
    store = InMemoryEventStore()
    bus_holder: dict[str, object] = {}

    async def inline_sink(cmd: Command, meta: EventMetadata) -> None:
        await bus_holder["bus"].dispatch(cmd, metadata=meta)  # type: ignore[attr-defined]

    bus = build_command_bus(store, saga_sink=inline_sink)
    bus_holder["bus"] = bus

    shot_repo: Repository[RenderShotAggregate] = Repository(store, RenderShotAggregate)
    # A shot that has been accepted (a settled clip the reader is now watching).
    await bus.dispatch(
        cc.PlanShot(shot_id="shot1", book_id="b", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="shot1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="shot1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    await bus.dispatch(cc.ScoreShotQA(shot_id="shot1", score=0.9, passed=True))

    # A director comment in Director mode targeting that shot.
    await bus.dispatch(cc.StartSession(session_id="sess1", user_id="u1", book_id="b"))
    await bus.dispatch(cc.SwitchMode(session_id="sess1", mode=SessionMode.DIRECTOR))
    await bus.dispatch(
        cc.LeaveDirectorComment(
            session_id="sess1",
            comment_id="cm1",
            shot_id="shot1",
            note="wrong room",
            routed_agent="continuity",
        )
    )

    shot = await shot_repo.load("shot1")
    assert shot.state is RenderState.PROMOTED  # the comment re-opened the shot
