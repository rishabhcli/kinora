"""Tests for the example projections + the registry/supervisor wiring."""

from __future__ import annotations

import asyncio

import pytest

from app.eventsourcing.projections.checkpoints import InMemoryCheckpointStore
from app.eventsourcing.projections.examples.canon_audit_view import (
    CanonAuditViewProjection,
)
from app.eventsourcing.projections.examples.session_timeline import (
    SessionTimelineProjection,
)
from app.eventsourcing.projections.examples.shot_status_board import (
    SUMMARY_KEY,
    ShotStatusBoardProjection,
)
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.readmodel import InMemoryReadModelStore
from app.eventsourcing.projections.registry import (
    ProjectionRegistry,
    ProjectionSupervisor,
    default_projections,
)
from app.eventsourcing.projections.runtime import ProjectionRuntime

pytestmark = pytest.mark.asyncio


def _stores():  # type: ignore[no-untyped-def]
    return InMemoryEventStore(), InMemoryReadModelStore(), InMemoryCheckpointStore()


async def _run(projection, es, rms, cps):  # type: ignore[no-untyped-def]
    rt = ProjectionRuntime(projection, event_store=es, read_models=rms, checkpoints=cps)
    await rt.catch_up()
    return rt


# --------------------------------------------------------------------------- #
# Session timeline
# --------------------------------------------------------------------------- #


async def test_session_timeline_folds_a_full_session() -> None:
    es, rms, cps = _stores()
    sid = "session:s1"
    await es.append(sid, "session.started", {"book_id": "b1", "reader_id": "u1"})
    await es.append(sid, "session.page_viewed", {"page": 3})
    await es.append(sid, "session.page_viewed", {"page": 1})
    await es.append(sid, "session.page_viewed", {"page": 7})
    await es.append(sid, "session.shot_played", {})
    await es.append(sid, "session.shot_played", {})
    await es.append(sid, "session.director_comment", {})
    await es.append(sid, "session.stalled", {})
    await es.append(sid, "session.ended", {"duration_s": 120.5})
    await _run(SessionTimelineProjection(), es, rms, cps)

    row = (await rms.get("session_timeline", sid)).value
    assert row["book_id"] == "b1"
    assert row["pages"] == [3, 1, 7]
    assert row["deepest_page"] == 7
    assert row["shots_played"] == 2
    assert row["director_comments"] == 1
    assert row["stalls"] == 1
    assert row["status"] == "ended"
    assert row["duration_s"] == 120.5


async def test_session_timeline_ignores_unrelated_events() -> None:
    es, rms, cps = _stores()
    await es.append("session:s1", "session.started", {})
    await es.append("session:s1", "shot.enqueued", {})  # not a session event
    await _run(SessionTimelineProjection(), es, rms, cps)
    row = (await rms.get("session_timeline", "session:s1")).value
    assert row["shots_played"] == 0


# --------------------------------------------------------------------------- #
# Shot status board
# --------------------------------------------------------------------------- #


async def test_shot_status_board_tracks_lifecycle_and_summary() -> None:
    es, rms, cps = _stores()
    # Shot 1: enqueued -> rendering -> qa -> accepted.
    await es.append("shot:1", "shot.enqueued", {"book_id": "b", "render_mode": "r2v"})
    await es.append("shot:1", "shot.render_started", {})
    await es.append("shot:1", "shot.qa_evaluated", {"score": 0.95})
    await es.append("shot:1", "shot.accepted", {})
    # Shot 2: enqueued -> rendering (still rendering).
    await es.append("shot:2", "shot.enqueued", {"book_id": "b"})
    await es.append("shot:2", "shot.render_started", {})
    await _run(ShotStatusBoardProjection(), es, rms, cps)

    s1 = (await rms.get("shot_status_board", "shot:1")).value
    assert s1["status"] == "accepted"
    assert s1["attempts"] == 1
    assert s1["qa_score"] == 0.95
    assert s1["render_mode"] == "r2v"

    summary = (await rms.get("shot_status_board", SUMMARY_KEY)).value
    assert summary["total"] == 2
    assert summary["counts"]["accepted"] == 1
    assert summary["counts"]["rendering"] == 1
    assert summary["counts"]["queued"] == 0


async def test_shot_status_board_counts_retries() -> None:
    es, rms, cps = _stores()
    await es.append("shot:1", "shot.enqueued", {})
    await es.append("shot:1", "shot.render_started", {})
    await es.append("shot:1", "shot.rejected", {})
    await es.append("shot:1", "shot.render_started", {})  # retry
    await es.append("shot:1", "shot.accepted", {})
    await _run(ShotStatusBoardProjection(), es, rms, cps)
    s1 = (await rms.get("shot_status_board", "shot:1")).value
    assert s1["attempts"] == 2
    assert s1["status"] == "accepted"
    summary = (await rms.get("shot_status_board", SUMMARY_KEY)).value
    assert summary["counts"]["accepted"] == 1
    assert summary["counts"]["rejected"] == 0  # moved out of rejected on accept


# --------------------------------------------------------------------------- #
# Canon audit view
# --------------------------------------------------------------------------- #


async def test_canon_audit_view_tracks_value_and_history() -> None:
    es, rms, cps = _stores()
    subj = "canon:char:alice"
    await es.append(
        subj,
        "canon.fact_asserted",
        {"predicate": "has", "value": "sword", "valid_from_beat": 20},
    )
    await es.append(subj, "canon.fact_corrected", {"value": "broken sword", "reason": "battle"})
    await es.append(subj, "canon.fact_retired", {"valid_to_beat": 50})
    await _run(CanonAuditViewProjection(), es, rms, cps)
    row = (await rms.get("canon_audit_view", subj)).value
    assert row["value"] == "broken sword"
    assert row["retired"] is True
    assert row["valid_to_beat"] == 50
    actions = [h["action"] for h in row["history"]]
    assert actions == ["assert", "correct", "retire"]
    assert row["revision"] == 3


async def test_canon_audit_view_records_conflict_resolution() -> None:
    es, rms, cps = _stores()
    subj = "canon:loc:castle"
    await es.append(subj, "canon.fact_asserted", {"value": "intact"})
    await es.append(subj, "canon.conflict_resolved", {"winner": "continuity", "value": "burned"})
    await _run(CanonAuditViewProjection(), es, rms, cps)
    row = (await rms.get("canon_audit_view", subj)).value
    assert row["value"] == "burned"
    assert row["history"][-1]["action"] == "conflict_resolved"
    assert row["history"][-1]["winner"] == "continuity"


# --------------------------------------------------------------------------- #
# Registry + supervisor
# --------------------------------------------------------------------------- #


async def test_registry_catches_up_all_default_projections() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)
    reg.register_all(default_projections())
    assert reg.names() == ["canon_audit_view", "session_timeline", "shot_status_board"]
    await es.append("session:s1", "session.started", {})
    await es.append("shot:1", "shot.enqueued", {})
    await es.append("canon:x", "canon.fact_asserted", {"value": 1})
    results = await reg.catch_up_all()
    assert results["session_timeline"].applied == 1
    assert results["shot_status_board"].applied == 1
    assert results["canon_audit_view"].applied == 1
    # All caught up to head -> lag 0.
    snaps = await reg.lag_snapshot()
    assert all(s.lag_events == 0 for s in snaps)


async def test_registry_rejects_duplicate_registration() -> None:
    es, rms, cps = _stores()
    reg = ProjectionRegistry(event_store=es, read_models=rms, checkpoints=cps)
    reg.register(SessionTimelineProjection())
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(SessionTimelineProjection())


async def test_supervisor_runs_and_stops_live_tails() -> None:
    es, rms, cps = _stores()
    from app.eventsourcing.projections.runtime import RuntimeConfig

    reg = ProjectionRegistry(
        event_store=es,
        read_models=rms,
        checkpoints=cps,
        config=RuntimeConfig(poll_interval_s=0.01),
    )
    reg.register(SessionTimelineProjection())
    supervisor = ProjectionSupervisor(reg)
    await supervisor.start()
    assert supervisor.running == ["session_timeline"]
    # Append after the tail is live.
    await es.append("session:s1", "session.started", {"book_id": "b1"})
    for _ in range(200):
        row = await rms.get("session_timeline", "session:s1")
        if row is not None:
            break
        await asyncio.sleep(0.01)
    await supervisor.stop()
    assert supervisor.running == []
    assert (await rms.get("session_timeline", "session:s1")) is not None
