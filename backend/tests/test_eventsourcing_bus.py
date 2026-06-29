"""Integration tests for the wired command bus: end-to-end command -> events,
metadata stamping, middleware ordering, idempotency, the §9.7 flow through the
bus, and the optimistic-concurrency retry under a racing writer."""

from __future__ import annotations

import pytest

from app.db.models.enums import SessionMode
from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.bus import CommandBus
from app.eventsourcing.domain.errors import AuthorizationError, CommandRejected, ValidationError
from app.eventsourcing.domain.events import EventMetadata
from app.eventsourcing.domain.identifiers import StreamId
from app.eventsourcing.domain.middleware import CommandContext
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.session import SessionAggregate
from app.eventsourcing.domain.wiring import build_command_bus
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.protocol import ConcurrencyError


def _bus(
    store: InMemoryEventStore | None = None, **kw: object
) -> tuple[CommandBus, InMemoryEventStore]:
    s = store or InMemoryEventStore()
    counter = {"n": 0}

    def ids() -> str:
        counter["n"] += 1
        return f"eid-{counter['n']}"

    bus = build_command_bus(s, id_factory=ids, **kw)  # type: ignore[arg-type]
    return bus, s


async def test_dispatch_appends_events_and_returns_result() -> None:
    bus, store = _bus()
    result = await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))
    assert result.event_types == ("SessionStarted",)
    assert result.new_version == 1
    assert result.stream_id == StreamId.session("s1")
    assert len(await store.load("session-s1")) == 1


async def test_metadata_is_stamped_on_events() -> None:
    bus, store = _bus()
    await bus.dispatch(
        cc.StartSession(session_id="s1", user_id="u1", book_id="b1"),
        metadata=EventMetadata(actor_id="u1", correlation_id="corr-1"),
    )
    (stored,) = await store.load("session-s1")
    assert stored.metadata["event_id"] == "eid-1"
    assert stored.metadata["actor_id"] == "u1"
    assert stored.metadata["correlation_id"] == "corr-1"
    assert "occurred_at" in stored.metadata


async def test_unknown_command_raises() -> None:
    bus, _ = _bus()

    class _Mystery(cc.StartSession):
        command_type = "Mystery"

    with pytest.raises(KeyError, match="no handler"):
        await bus.dispatch(_Mystery(session_id="s", user_id="u", book_id="b"))


async def test_validation_rejection_surfaces() -> None:
    bus, _ = _bus()
    with pytest.raises(ValidationError):
        await bus.dispatch(cc.StartSession(session_id="", user_id="u", book_id="b"))


async def test_business_rejection_surfaces() -> None:
    bus, _ = _bus()
    await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))
    with pytest.raises(CommandRejected, match="already started"):
        await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))


async def test_auth_policy_blocks_command() -> None:
    def deny_director(cmd: object, _meta: EventMetadata) -> bool:
        return not isinstance(cmd, cc.SwitchMode)

    bus, _ = _bus(auth_policy=deny_director)
    await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))
    with pytest.raises(AuthorizationError):
        await bus.dispatch(cc.SwitchMode(session_id="s1", mode=SessionMode.DIRECTOR))


async def test_idempotent_render_does_not_double_append() -> None:
    bus, store = _bus()
    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    r1 = await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    version_after_first = await store.current_version("rendershot-sh1")
    r2 = await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    assert r1.idempotent_replay is False
    assert r2.idempotent_replay is True
    # The stream did not grow on the replay -> no double-spend.
    assert await store.current_version("rendershot-sh1") == version_after_first


async def test_full_9_7_happy_path_through_bus() -> None:
    bus, store = _bus()
    await bus.dispatch(
        cc.PlanShot(shot_id="sh1", book_id="b", scene_id="s", beat_id="be", shot_hash="h1")
    )
    await bus.dispatch(cc.KeyframeShot(shot_id="sh1", keyframe_url="kf"))
    await bus.dispatch(cc.PromoteShot(shot_id="sh1"))
    await bus.dispatch(
        cc.RenderShot(
            shot_id="sh1", shot_hash="h1", cache_hit=False, clip_url="c", video_seconds=5.0
        )
    )
    result = await bus.dispatch(cc.ScoreShotQA(shot_id="sh1", score=0.95, passed=True))
    # The QA handler emits the verdict + the accept transition + the acceptance.
    assert "ShotAccepted" in result.event_types
    types = [e.event_type for e in await store.load("rendershot-sh1")]
    assert types[0] == "ShotPlanned"
    assert types[-1] == "ShotAccepted"


async def test_concurrency_retry_recovers_under_racing_writer() -> None:
    store = InMemoryEventStore()
    # Pre-create the session so both writers load a non-empty stream.
    bus0, _ = _bus(store)
    await bus0.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))

    # A bus whose handler injects a one-shot conflicting write *before* the first
    # save, to force exactly one ConcurrencyError that the retry then recovers from.
    bus, _ = _bus(store)

    repo: Repository[SessionAggregate] = Repository(store, SessionAggregate)
    injected = {"done": False}
    original_handler = bus._registry[cc.RecordPreference.command_type].handler

    async def racing_handler(command: object, r: object) -> object:
        agg = await original_handler(command, r)  # type: ignore[arg-type]
        if not injected["done"]:
            injected["done"] = True
            # Another writer commits first, bumping the stream version.
            other = await repo.load("s1")
            other.switch_mode(mode=SessionMode.DIRECTOR)
            await repo.save(other)
        return agg

    bus._registry[cc.RecordPreference.command_type].handler = racing_handler  # type: ignore[assignment]

    result = await bus.dispatch(cc.RecordPreference(session_id="s1", key="pacing", value="slow"))
    # Despite the injected race, the retry re-loaded + re-decided and committed.
    assert result.event_types == ("PreferenceRecorded",)
    final = await repo.load("s1")
    assert final.preferences == {"pacing": "slow"}
    assert final.mode is SessionMode.DIRECTOR  # the racing writer's effect persisted too


async def test_retry_gives_up_after_policy_exhausted() -> None:
    from app.eventsourcing.domain.concurrency import RetryPolicy

    store = InMemoryEventStore()
    bus, _ = _bus(store, retry_policy=RetryPolicy(max_attempts=1))
    await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))

    repo: Repository[SessionAggregate] = Repository(store, SessionAggregate)
    original = bus._registry[cc.RecordPreference.command_type].handler

    async def always_racing(command: object, r: object) -> object:
        agg = await original(command, r)  # type: ignore[arg-type]
        other = await repo.load("s1")
        other.update_intent(focus_word=999, velocity=9.0)
        await repo.save(other)
        return agg

    bus._registry[cc.RecordPreference.command_type].handler = always_racing  # type: ignore[assignment]
    with pytest.raises(ConcurrencyError):
        await bus.dispatch(cc.RecordPreference(session_id="s1", key="p", value="v"))


async def test_double_handler_registration_rejected() -> None:
    bus, _ = _bus()

    async def _stub(command: object, repo: object) -> object:  # pragma: no cover - never run
        raise AssertionError("stub handler should not run")

    with pytest.raises(ValueError, match="already has a handler"):
        bus.register(cc.StartSession.command_type, _stub, None)  # type: ignore[arg-type]


async def test_middleware_runs_outermost_first() -> None:
    order: list[str] = []

    class _Recorder:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def handle(self, ctx: CommandContext, next_: object) -> object:
            order.append(f"{self.tag}-in")
            result = await next_(ctx)  # type: ignore[operator]
            order.append(f"{self.tag}-out")
            return result

    store = InMemoryEventStore()
    bus = build_command_bus(store, extra_middleware=[_Recorder("X")])  # type: ignore[list-item]
    # Manually prepend an outer recorder to assert ordering.
    bus.middleware.insert(0, _Recorder("OUTER"))  # type: ignore[arg-type]
    await bus.dispatch(cc.StartSession(session_id="s1", user_id="u1", book_id="b1"))
    assert order.index("OUTER-in") < order.index("X-in")
    assert order.index("X-out") < order.index("OUTER-out")
