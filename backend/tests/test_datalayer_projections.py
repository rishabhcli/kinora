"""Exhaustive, infra-free tests for the consolidated read side (``app/datalayer``).

Everything runs against the real :class:`InMemoryEventStore` (the event-store
contract this read side consumes) plus the in-memory read-model / checkpoint
stores — zero infrastructure, fully deterministic. Events are appended in the
*exact* domain-envelope shape :func:`app.eventsourcing.domain.events.serialise`
produces (``{"type","version","data","meta"}``) so the decode path is exercised
end to end.

Coverage:

* envelope decode (envelope vs bare payload, metadata fallback);
* projection apply for all three product read models;
* checkpoint advance / resume / monotonicity / applied-event dedupe;
* runner catch-up paging, idempotent re-run, and crash-resume;
* rebuild-from-zero idempotency (rebuild == catch-up == double-rebuild);
* the consistency checker (clean view passes; a corrupted live view is caught);
* the registry admin surface (``rebuild_projection`` + ``UnknownProjectionError``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.datalayer.checkpoints import (
    InMemoryCheckpointStore,
    ProjectionStatus,
)
from app.datalayer.consistency import check_consistency
from app.datalayer.envelope import decode
from app.datalayer.projector import (
    Projection,
    ProjectionRunner,
    RunnerConfig,
    collect_handler_types,
    handles,
)
from app.datalayer.readmodel import InMemoryReadModelStore
from app.datalayer.readmodels import all_projections
from app.datalayer.readmodels.render_progress import (
    RenderProgressProjection,
    RenderProgressRepository,
)
from app.datalayer.readmodels.session_activity import (
    SessionActivityProjection,
    SessionActivityRepository,
)
from app.datalayer.readmodels.shot_lifecycle import (
    SUMMARY_KEY,
    ShotLifecycleProjection,
    ShotLifecycleRepository,
)
from app.datalayer.registry import (
    ProjectionRegistry,
    UnknownProjectionError,
    build_default_registry,
)
from app.eventsourcing.store.contracts import EventData
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.versioning import ANY

# --------------------------------------------------------------------------- #
# Helpers — build + append events in the real domain-envelope shape
# --------------------------------------------------------------------------- #

_counter = {"n": 0}


def _eid() -> str:
    _counter["n"] += 1
    return f"evt-{_counter['n']:04d}"


def _envelope(event_type: str, data: dict[str, Any], *, actor: str | None = None) -> EventData:
    """An EventData whose payload is the domain envelope ``serialise`` produces."""
    meta: dict[str, Any] = {}
    if actor is not None:
        meta["actor_id"] = actor
    return EventData(
        event_type=event_type,
        payload={"type": event_type, "version": 1, "data": data, "meta": meta},
        event_id=_eid(),
    )


async def _append(
    store: InMemoryEventStore, stream_id: str, *events: EventData
) -> None:
    await store.append(stream_id, list(events), expected_version=ANY)


def _shot_stream(shot_id: str) -> str:
    return f"render_shot-{shot_id}"


def _session_stream(session_id: str) -> str:
    return f"session-{session_id}"


async def _seed_render_book(
    store: InMemoryEventStore,
    *,
    book_id: str = "book_1",
    accepted: Sequence[str] = (),
    degraded: Sequence[str] = (),
    planned_only: Sequence[str] = (),
    seconds: float = 5.0,
) -> None:
    """Seed a book's worth of shot streams with planned/rendered/terminal events."""
    for shot_id in (*accepted, *degraded, *planned_only):
        await _append(
            store,
            _shot_stream(shot_id),
            _envelope("ShotPlanned", {"shot_id": shot_id, "book_id": book_id}),
        )
    for shot_id in (*accepted, *degraded):
        await _append(
            store,
            _shot_stream(shot_id),
            _envelope("ShotRendered", {"shot_id": shot_id, "video_seconds": seconds}),
        )
    for shot_id in accepted:
        await _append(
            store,
            _shot_stream(shot_id),
            _envelope("ShotAccepted", {"shot_id": shot_id, "clip_url": f"s3://{shot_id}.mp4"}),
        )
    for shot_id in degraded:
        await _append(
            store,
            _shot_stream(shot_id),
            _envelope("ShotDegraded", {"shot_id": shot_id, "reason": "retries_exhausted"}),
        )


# --------------------------------------------------------------------------- #
# Envelope decode
# --------------------------------------------------------------------------- #


async def test_decode_unwraps_domain_envelope() -> None:
    store = InMemoryEventStore()
    await _append(
        store,
        _shot_stream("s1"),
        _envelope("ShotPlanned", {"shot_id": "s1", "book_id": "b1"}, actor="scheduler"),
    )
    (recorded,) = await store.read_all(from_position=0, limit=10)
    event = decode(recorded)
    assert event.type == "ShotPlanned"
    assert event.data == {"shot_id": "s1", "book_id": "b1"}
    assert event.actor == "scheduler"
    assert event.global_position == 1
    assert event.stream_version == 0


async def test_decode_handles_bare_payload() -> None:
    """A non-envelope payload falls back to event_type + raw payload."""
    store = InMemoryEventStore()
    await store.append(
        "misc-1",
        [EventData(event_type="raw.thing", payload={"foo": "bar"}, event_id=_eid())],
        expected_version=ANY,
    )
    (recorded,) = await store.read_all(from_position=0, limit=10)
    event = decode(recorded)
    assert event.type == "raw.thing"
    assert event.data == {"foo": "bar"}


async def test_projection_event_equality_is_by_event_id() -> None:
    store = InMemoryEventStore()
    await _append(store, _shot_stream("s1"), _envelope("ShotPlanned", {"shot_id": "s1"}))
    (recorded,) = await store.read_all(from_position=0, limit=10)
    a = decode(recorded)
    b = decode(recorded)
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


# --------------------------------------------------------------------------- #
# RenderProgress projection
# --------------------------------------------------------------------------- #


async def test_render_progress_counts_and_percent() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(
        store, accepted=["s1", "s2"], degraded=["s3"], planned_only=["s4"], seconds=4.0
    )
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        RenderProgressProjection(),
        event_store=store,
        read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()

    repo = RenderProgressRepository(rm)
    row = await repo.for_book("book_1")
    assert row is not None
    assert row["shots_planned"] == 4
    assert row["shots_accepted"] == 2
    assert row["shots_degraded"] == 1
    assert row["shots_settled"] == 3
    assert row["percent_complete"] == 75.0
    # Only the 3 rendered shots contributed seconds (s4 was planned-only).
    assert row["video_seconds"] == pytest.approx(12.0)
    # Internal bookkeeping is hidden from the read facade.
    assert "shots" not in row


async def test_render_progress_regen_reopens_settled_shot() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"])
    await _append(
        store,
        _shot_stream("s1"),
        _envelope("ShotRegenRequested", {"shot_id": "s1", "reason": "director_note"}),
    )
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        RenderProgressProjection(), event_store=store, read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()
    row = await RenderProgressRepository(rm).for_book("book_1")
    assert row is not None
    assert row["shots_accepted"] == 0  # the regen re-opened it
    assert row["shots_settled"] == 0
    assert row["percent_complete"] == 0.0


# --------------------------------------------------------------------------- #
# SessionActivity projection
# --------------------------------------------------------------------------- #


async def test_session_activity_folds_lifecycle() -> None:
    store = InMemoryEventStore()
    sid = "sess_1"
    await _append(
        store,
        _session_stream(sid),
        _envelope(
            "SessionStarted",
            {"session_id": sid, "user_id": "u1", "book_id": "b1", "mode": "viewer"},
        ),
        _envelope("IntentUpdated", {"session_id": sid, "focus_word": 42, "velocity": 1.5}),
        _envelope("ModeSwitched", {"session_id": sid, "mode": "director"}),
        _envelope(
            "DirectorCommentLeft",
            {"session_id": sid, "comment_id": "c1", "shot_id": "shot_9", "note": "warmer"},
        ),
        _envelope("PreferenceRecorded", {"session_id": sid, "key": "palette", "value": "warm"}),
    )
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        SessionActivityProjection(), event_store=store, read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()

    repo = SessionActivityRepository(rm)
    row = await repo.for_session(sid)
    assert row is not None
    assert row["user_id"] == "u1"
    assert row["book_id"] == "b1"
    assert row["mode"] == "director"
    assert row["focus_word"] == 42
    assert row["velocity"] == pytest.approx(1.5)
    assert row["comment_count"] == 1
    assert row["preference_count"] == 1
    assert row["status"] == "active"
    assert "comment_ids" not in row and "preference_keys" not in row

    assert len(await repo.active_sessions()) == 1
    assert len(await repo.for_book("b1")) == 1

    # End the session; it leaves the active set.
    await _append(
        store,
        _session_stream(sid),
        _envelope("SessionEnded", {"session_id": sid, "reason": "idle_swept"}),
    )
    await runner.catch_up()
    ended = await repo.for_session(sid)
    assert ended is not None and ended["status"] == "ended"
    assert ended["ended_reason"] == "idle_swept"
    assert await repo.active_sessions() == []


async def test_session_activity_comment_count_is_replay_safe() -> None:
    """Re-appending the same comment id (or replaying) must not inflate the count."""
    store = InMemoryEventStore()
    sid = "sess_2"
    proj = SessionActivityProjection()
    rm = InMemoryReadModelStore()
    await _append(
        store,
        _session_stream(sid),
        _envelope("SessionStarted", {"session_id": sid, "user_id": "u", "book_id": "b"}),
    )
    comment = _envelope("DirectorCommentLeft", {"session_id": sid, "comment_id": "dup"})
    await _append(store, _session_stream(sid), comment)
    # Apply the same decoded comment twice directly (idempotency of the fold).
    (started, left) = await store.read_all(from_position=0, limit=10)
    ev = decode(left)
    await proj.apply(rm, proj.namespace, ev)
    await proj.apply(rm, proj.namespace, ev)
    row = await rm.get(proj.namespace, sid)
    assert row is not None
    assert row.value["comment_count"] == 1


# --------------------------------------------------------------------------- #
# ShotLifecycle projection
# --------------------------------------------------------------------------- #


async def test_shot_lifecycle_board_and_summary() -> None:
    store = InMemoryEventStore()
    await _append(
        store,
        _shot_stream("s1"),
        _envelope("ShotPlanned", {"shot_id": "s1", "book_id": "b1"}),
        _envelope("ShotKeyframed", {"shot_id": "s1", "keyframe_url": "kf"}),
        _envelope(
            "ShotTransitioned", {"shot_id": "s1", "to_state": "Rendering", "reason": "cache_miss"}
        ),
        _envelope("ShotRendered", {"shot_id": "s1", "clip_url": "clip", "video_seconds": 5.0}),
        _envelope("ShotQAScored", {"shot_id": "s1", "score": 0.9, "passed": True}),
        _envelope("ShotAccepted", {"shot_id": "s1", "clip_url": "clip"}),
    )
    await _append(
        store,
        _shot_stream("s2"),
        _envelope("ShotPlanned", {"shot_id": "s2", "book_id": "b1"}),
        _envelope("ShotDegraded", {"shot_id": "s2", "reason": "qa_fail"}),
    )
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        ShotLifecycleProjection(), event_store=store, read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()

    repo = ShotLifecycleRepository(rm)
    s1 = await repo.for_shot("s1")
    assert s1 is not None
    assert s1["state"] == "Accepted"
    assert s1["attempts"] == 1
    assert s1["qa_score"] == 0.9
    assert s1["qa_passed"] is True
    assert s1["video_seconds"] == pytest.approx(5.0)
    assert s1["keyframe_url"] == "kf"

    summary = await repo.summary()
    assert summary["total"] == 2
    assert summary["counts"]["Accepted"] == 1
    assert summary["counts"]["Degraded"] == 1
    # The summary row is excluded from the board.
    board = await repo.board()
    assert {r["shot_id"] for r in board} == {"s1", "s2"}
    assert SUMMARY_KEY not in {r["shot_id"] for r in board}
    assert len(await repo.in_state("Accepted")) == 1


# --------------------------------------------------------------------------- #
# Checkpoint store semantics
# --------------------------------------------------------------------------- #


async def test_checkpoint_advance_is_monotonic() -> None:
    cp = InMemoryCheckpointStore()
    await cp.advance("p", 5)
    after_back = await cp.advance("p", 3)  # stale, must not move backwards
    assert after_back.position == 5
    forward = await cp.advance("p", 9)
    assert forward.position == 9


async def test_checkpoint_applied_dedupe() -> None:
    cp = InMemoryCheckpointStore()
    assert await cp.mark_applied("p", "e1") is True
    assert await cp.mark_applied("p", "e1") is False
    assert await cp.was_applied("p", "e1") is True
    assert await cp.was_applied("p", "e2") is False
    # reset clears the applied set + position.
    await cp.advance("p", 7)
    reset = await cp.reset("p")
    assert reset.position == 0
    assert reset.status is ProjectionStatus.CATCHING_UP
    assert await cp.was_applied("p", "e1") is False


# --------------------------------------------------------------------------- #
# Runner: catch-up, resume, idempotency
# --------------------------------------------------------------------------- #


async def test_catch_up_advances_checkpoint_and_is_idempotent() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1", "s2"])
    rm = InMemoryReadModelStore()
    cp = InMemoryCheckpointStore()
    runner = ProjectionRunner(
        RenderProgressProjection(), event_store=store, read_models=rm, checkpoints=cp
    )

    first = await runner.catch_up()
    head = await store.last_position()
    assert first.to_position == head
    assert first.applied == head  # every event newly applied
    assert first.skipped == 0
    checkpoint = await runner.checkpoint()
    assert checkpoint.position == head
    assert checkpoint.status is ProjectionStatus.LIVE
    assert checkpoint.events_applied == head

    # Re-running with no new events does nothing (all already applied).
    second = await runner.catch_up()
    assert second.applied == 0
    assert second.skipped == 0  # nothing read past the checkpoint
    assert second.to_position == head

    row = await RenderProgressRepository(rm).for_book("book_1")
    assert row is not None and row["shots_accepted"] == 2


async def test_catch_up_resumes_from_checkpoint_after_new_events() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"])
    rm = InMemoryReadModelStore()
    cp = InMemoryCheckpointStore()
    runner = ProjectionRunner(
        ShotLifecycleProjection(), event_store=store, read_models=rm, checkpoints=cp
    )
    r1 = await runner.catch_up()
    pos1 = r1.to_position

    # Append more, then catch up again — only the new tail is applied.
    await _append(
        store,
        _shot_stream("s9"),
        _envelope("ShotPlanned", {"shot_id": "s9", "book_id": "book_1"}),
    )
    r2 = await runner.catch_up()
    assert r2.from_position == pos1
    assert r2.applied == 1
    assert r2.to_position == await store.last_position()


async def test_resume_does_not_double_apply_after_crash() -> None:
    """A checkpoint that lags one event behind must replay exactly that event once.

    Simulates: the read model was updated but the process died before advancing
    past the last event. The applied-event ledger has it, so the replay skips it.
    """
    store = InMemoryEventStore()
    await _append(
        store,
        _shot_stream("s1"),
        _envelope("ShotPlanned", {"shot_id": "s1", "book_id": "b"}),
    )
    await _append(
        store,
        _shot_stream("s1"),
        _envelope("ShotRendered", {"shot_id": "s1", "video_seconds": 3.0}),
    )
    rm = InMemoryReadModelStore()
    cp = InMemoryCheckpointStore()
    proj = ShotLifecycleProjection()
    runner = ProjectionRunner(proj, event_store=store, read_models=rm, checkpoints=cp)
    await runner.catch_up()
    attempts_before = (await rm.get(proj.namespace, "s1")).value["attempts"]  # type: ignore[union-attr]

    # Simulate a crash where the read model + applied-event ledger are durable but
    # the position advance was lost: build a *fresh* checkpoint store that already
    # records both events as applied (the ledger survived) yet sits at position 0.
    # A naive replay would double-count attempts; the per-event dedupe prevents it.
    crashed_cp = InMemoryCheckpointStore()
    for recorded in await store.read_all(from_position=0, limit=100):
        await crashed_cp.mark_applied(proj.name, recorded.event_id)
    resumed = ProjectionRunner(proj, event_store=store, read_models=rm, checkpoints=crashed_cp)
    result = await resumed.catch_up()
    assert result.applied == 0  # every event already in the ledger => skipped
    assert result.skipped == await store.last_position()
    attempts_after = (await rm.get(proj.namespace, "s1")).value["attempts"]  # type: ignore[union-attr]
    assert attempts_after == attempts_before == 1


async def test_catch_up_pages_in_small_batches() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(
        store, accepted=[f"s{i}" for i in range(10)], seconds=1.0
    )
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        RenderProgressProjection(),
        event_store=store,
        read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
        config=RunnerConfig(batch_size=3),  # force many pages
    )
    result = await runner.catch_up()
    assert result.to_position == await store.last_position()
    row = await RenderProgressRepository(rm).for_book("book_1")
    assert row is not None and row["shots_accepted"] == 10


# --------------------------------------------------------------------------- #
# Rebuild-from-zero idempotency
# --------------------------------------------------------------------------- #


async def test_rebuild_matches_catch_up_and_is_idempotent() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1", "s2"], degraded=["s3"])
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        RenderProgressProjection(), event_store=store, read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()
    after_catch_up = rm.snapshot("render_progress")

    r1 = await runner.rebuild()
    after_rebuild = rm.snapshot("render_progress")
    r2 = await runner.rebuild()
    after_second_rebuild = rm.snapshot("render_progress")

    assert after_catch_up == after_rebuild == after_second_rebuild
    assert r1.applied == r2.applied == await store.last_position()
    # The checkpoint is fully caught up + LIVE after a rebuild.
    cp = await runner.checkpoint()
    assert cp.position == await store.last_position()
    assert cp.status is ProjectionStatus.LIVE


async def test_rebuild_clears_stale_rows() -> None:
    """Rows written before a rebuild that the replay does not re-create are dropped."""
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"])
    rm = InMemoryReadModelStore()
    runner = ProjectionRunner(
        RenderProgressProjection(), event_store=store, read_models=rm,
        checkpoints=InMemoryCheckpointStore(),
    )
    await runner.catch_up()
    # Inject a bogus row not derivable from any event.
    await rm.put("render_progress", "ghost_book", {"book_id": "ghost_book", "shots_planned": 99})
    assert await rm.get("render_progress", "ghost_book") is not None
    await runner.rebuild()
    assert await rm.get("render_progress", "ghost_book") is None


# --------------------------------------------------------------------------- #
# Consistency checker
# --------------------------------------------------------------------------- #


async def test_consistency_passes_for_a_clean_view() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1", "s2"], degraded=["s3"], planned_only=["s4"])
    rm = InMemoryReadModelStore()
    proj = RenderProgressProjection()
    runner = ProjectionRunner(
        proj, event_store=store, read_models=rm, checkpoints=InMemoryCheckpointStore()
    )
    await runner.catch_up()

    report = await check_consistency(proj, event_store=store, live_read_models=rm)
    assert report.consistent
    assert report.diffs == []
    assert report.actual_rows == report.expected_rows == 1
    assert report.events_replayed == await store.last_position()
    assert "consistent" in report.summary()


async def test_consistency_detects_corruption() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"])
    rm = InMemoryReadModelStore()
    proj = RenderProgressProjection()
    runner = ProjectionRunner(
        proj, event_store=store, read_models=rm, checkpoints=InMemoryCheckpointStore()
    )
    await runner.catch_up()

    # Corrupt the live view three ways: mismatch + extra + missing.
    live = await rm.get("render_progress", "book_1")
    assert live is not None
    bad = dict(live.value)
    bad["shots_accepted"] = 999
    await rm.put("render_progress", "book_1", bad)
    await rm.put("render_progress", "extra_book", {"book_id": "extra_book"})

    report = await check_consistency(proj, event_store=store, live_read_models=rm)
    assert not report.consistent
    kinds = {d.kind for d in report.diffs}
    assert "mismatch" in kinds  # book_1 differs
    assert "extra" in kinds  # extra_book has no events
    assert "INCONSISTENT" in report.summary()


async def test_consistency_is_read_only() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"])
    rm = InMemoryReadModelStore()
    proj = RenderProgressProjection()
    runner = ProjectionRunner(
        proj, event_store=store, read_models=rm, checkpoints=InMemoryCheckpointStore()
    )
    await runner.catch_up()
    before = rm.snapshot("render_progress")
    await check_consistency(proj, event_store=store, live_read_models=rm)
    assert rm.snapshot("render_progress") == before


# --------------------------------------------------------------------------- #
# Registry admin surface
# --------------------------------------------------------------------------- #


async def test_registry_catch_up_all_and_rebuild() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1", "s2"])
    await _append(
        store,
        _session_stream("sess_1"),
        _envelope("SessionStarted", {"session_id": "sess_1", "user_id": "u", "book_id": "book_1"}),
    )
    registry = build_default_registry(event_store=store)
    assert registry.names() == ["render_progress", "session_activity", "shot_lifecycle"]

    results = await registry.catch_up_all()
    head = await store.last_position()
    assert all(r.to_position == head for r in results.values())

    # rebuild_projection on a known name returns a fully caught-up result.
    rb = await registry.rebuild_projection("render_progress")
    assert rb.to_position == head

    # The read facade over the shared store sees the data.
    progress = RenderProgressRepository(registry.read_models)
    row = await progress.for_book("book_1")
    assert row is not None and row["shots_accepted"] == 2

    lag = await registry.lag()
    assert lag["render_progress"] == 0  # caught up


async def test_registry_unknown_projection_raises() -> None:
    store = InMemoryEventStore()
    registry = build_default_registry(event_store=store)
    with pytest.raises(UnknownProjectionError):
        await registry.rebuild_projection("does_not_exist")
    with pytest.raises(UnknownProjectionError):
        registry.get("nope")


async def test_registry_rejects_duplicate_names() -> None:
    store = InMemoryEventStore()
    registry = ProjectionRegistry(
        event_store=store,
        read_models=InMemoryReadModelStore(),
        checkpoints=InMemoryCheckpointStore(),
    )
    registry.register(RenderProgressProjection())
    with pytest.raises(ValueError):
        registry.register(RenderProgressProjection())


async def test_registry_rebuild_all_round_trips() -> None:
    store = InMemoryEventStore()
    await _seed_render_book(store, accepted=["s1"], degraded=["s2"])
    registry = build_default_registry(event_store=store)
    await registry.catch_up_all()
    before = {
        name: registry.read_models.snapshot(name)  # type: ignore[attr-defined]
        for name in registry.names()
    }
    await registry.rebuild_all()
    after = {
        name: registry.read_models.snapshot(name)  # type: ignore[attr-defined]
        for name in registry.names()
    }
    assert before == after


# --------------------------------------------------------------------------- #
# Projection contract sanity
# --------------------------------------------------------------------------- #


async def test_handles_registry_and_interested_in() -> None:
    proj = ShotLifecycleProjection()
    types = set(collect_handler_types(proj))
    assert {"ShotPlanned", "ShotRendered", "ShotAccepted", "ShotDegraded"} <= types
    assert proj.interested_in() == frozenset(types)


def test_duplicate_handler_for_same_type_is_rejected() -> None:
    with pytest.raises(ValueError):

        class _Bad(Projection):
            name = "bad"

            @handles("X")
            async def a(self, store: Any, namespace: str, event: Any) -> None: ...

            @handles("X")
            async def b(self, store: Any, namespace: str, event: Any) -> None: ...


async def test_all_projections_have_unique_names_and_handlers() -> None:
    projs = all_projections()
    names = [p.name for p in projs]  # type: ignore[attr-defined]
    assert len(names) == len(set(names))
    for p in projs:
        assert list(collect_handler_types(p))  # type: ignore[arg-type]


async def test_runner_records_error_and_faults_on_handler_exception() -> None:
    """A raising handler stops the runner, marks the event applied, faults the checkpoint."""

    class _Boom(Projection):
        name = "boom"

        @handles("Boom")
        async def _on(self, store: Any, namespace: str, event: Any) -> None:
            raise RuntimeError("kaboom")

    store = InMemoryEventStore()
    await _append(store, "x-1", _envelope("Boom", {}))
    cp = InMemoryCheckpointStore()
    runner = ProjectionRunner(
        _Boom(), event_store=store, read_models=InMemoryReadModelStore(), checkpoints=cp
    )
    with pytest.raises(RuntimeError, match="kaboom"):
        await runner.catch_up()
    checkpoint = await cp.load("boom")
    assert checkpoint.status is ProjectionStatus.FAULTED
    assert checkpoint.error_count == 1
    assert checkpoint.last_error is not None and "kaboom" in checkpoint.last_error
    # The failing event is NOT poisoned into the applied ledger, so a fixed
    # handler could retry it (apply-before-mark ordering).
    (recorded,) = await store.read_all(from_position=0, limit=10)
    assert await cp.was_applied("boom", recorded.event_id) is False
