"""Command handlers — the thin glue from a command to an aggregate decision.

Each handler loads (or builds) the target aggregate via the repository, calls the
matching pure decision method with the command's data, and returns the aggregate
with its uncommitted events queued. The :class:`~app.eventsourcing.domain.bus.CommandBus`
owns the append + metadata stamping + saga fan-out, so handlers carry no IO of
their own beyond the load.

Handlers are intentionally boring: all the *logic* lives in the aggregates'
decision methods, which is what keeps the write side exhaustively testable
without a bus at all.
"""

from __future__ import annotations

from typing import cast

from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.canon import CanonEntityAggregate
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.errors import CommandRejected
from app.eventsourcing.domain.render_shot import MAX_REPAIRS, RenderShotAggregate
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.session import SessionAggregate

_Repo = Repository[AggregateRoot]


# --------------------------------------------------------------------------- #
# Session handlers
# --------------------------------------------------------------------------- #


async def handle_start_session(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.StartSession, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.start(user_id=cmd.user_id, book_id=cmd.book_id)
    return agg


async def handle_update_intent(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.UpdateIntent, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.update_intent(focus_word=cmd.focus_word, velocity=cmd.velocity)
    return agg


async def handle_switch_mode(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.SwitchMode, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.switch_mode(mode=cmd.mode)
    return agg


async def handle_leave_comment(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.LeaveDirectorComment, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.leave_comment(
        comment_id=cmd.comment_id,
        shot_id=cmd.shot_id,
        note=cmd.note,
        routed_agent=cmd.routed_agent,
        region=cmd.region,
    )
    return agg


async def handle_record_preference(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.RecordPreference, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.record_preference(key=cmd.key, value=cmd.value)
    return agg


async def handle_end_session(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.EndSession, command)
    agg = cast(SessionAggregate, await repo.load(cmd.session_id))
    agg.end(reason=cmd.reason)
    return agg


# --------------------------------------------------------------------------- #
# Render-shot handlers
# --------------------------------------------------------------------------- #


async def handle_plan_shot(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.PlanShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.plan(
        book_id=cmd.book_id,
        scene_id=cmd.scene_id,
        beat_id=cmd.beat_id,
        shot_hash=cmd.shot_hash,
    )
    return agg


async def handle_keyframe_shot(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.KeyframeShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.keyframe(keyframe_url=cmd.keyframe_url)
    return agg


async def handle_promote_shot(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.PromoteShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.promote()
    return agg


async def handle_render_shot(command: Command, repo: _Repo) -> AggregateRoot:
    """CacheCheck -> Accepted (hit) or Rendering -> QA (miss), per §9.7."""
    cmd = cast(cc.RenderShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.begin_cache_check()
    if cmd.cache_hit:
        agg.cache_hit(clip_url=cmd.clip_url)
    else:
        agg.cache_miss()
        agg.rendered(clip_url=cmd.clip_url, video_seconds=cmd.video_seconds)
    return agg


async def handle_score_qa(command: Command, repo: _Repo) -> AggregateRoot:
    """Record QA, then take the QA -> (Accepted|Repair) edge it implies (§9.7)."""
    cmd = cast(cc.ScoreShotQA, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.score_qa(score=cmd.score, passed=cmd.passed, reason=cmd.reason)
    if cmd.passed:
        agg.accept_after_qa()
    else:
        agg.repair()
    return agg


async def handle_repair_shot(command: Command, repo: _Repo) -> AggregateRoot:
    """Apply the §9.7 Repair routing: regen while under the cap, else degrade.

    A contradiction id routes to Conflict; otherwise a regen is attempted, and
    once the ``<= 2`` retry cap is hit the shot degrades to the Ken-Burns ladder.
    """
    cmd = cast(cc.RepairShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    if cmd.contradiction_state_id:
        agg.raise_conflict(
            contradicting_state_id=cmd.contradiction_state_id,
            detail=cmd.conflict_detail,
        )
    elif agg.repair_count >= MAX_REPAIRS:
        agg.degrade(reason="retries_exhausted")
    else:
        agg.regen()
    return agg


async def handle_regenerate_shot(command: Command, repo: _Repo) -> AggregateRoot:
    """§5.4 surgical re-do: re-open the shot to Promoted (no-op if already in flow)."""
    cmd = cast(cc.RegenerateShot, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    agg.request_regen(reason=cmd.reason, triggered_by=cmd.triggered_by)
    return agg


async def handle_resolve_conflict(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.ResolveShotConflict, command)
    agg = cast(RenderShotAggregate, await repo.load(cmd.shot_id))
    if cmd.decision == "regen":
        agg.resolve_conflict_regen()
    elif cmd.decision == "accept":
        agg.resolve_conflict_accept()
    elif cmd.decision == "degrade":
        agg.degrade(reason="conflict_unresolved")
    else:  # pragma: no cover - guarded by validation
        raise CommandRejected(f"unknown conflict decision {cmd.decision!r}")
    return agg


# --------------------------------------------------------------------------- #
# Canon handlers
# --------------------------------------------------------------------------- #


async def handle_register_canon(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.RegisterCanonEntity, command)
    agg = cast(CanonEntityAggregate, await repo.load(cmd.entity_id))
    agg.register(book_id=cmd.book_id, entity_type=cmd.entity_type, name=cmd.name)
    return agg


async def handle_edit_canon_field(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.EditCanonField, command)
    agg = cast(CanonEntityAggregate, await repo.load(cmd.entity_id))
    agg.edit_field(
        field_name=cmd.field_name,
        new_value=cmd.new_value,
        dependent_shot_ids=cmd.dependent_shot_ids,
        expected_canon_version=cmd.expected_canon_version,
    )
    return agg


async def handle_swap_reference(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.SwapReferenceImage, command)
    agg = cast(CanonEntityAggregate, await repo.load(cmd.entity_id))
    agg.swap_reference_image(
        new_reference_id=cmd.new_reference_id,
        dependent_shot_ids=cmd.dependent_shot_ids,
        expected_canon_version=cmd.expected_canon_version,
    )
    return agg


async def handle_evolve_canon(command: Command, repo: _Repo) -> AggregateRoot:
    cmd = cast(cc.EvolveCanonFromConflict, command)
    agg = cast(CanonEntityAggregate, await repo.load(cmd.entity_id))
    agg.evolve_from_conflict(
        field_name=cmd.field_name,
        new_value=cmd.new_value,
        conflict_id=cmd.conflict_id,
        dependent_shot_ids=cmd.dependent_shot_ids,
    )
    return agg


__all__ = [
    "handle_edit_canon_field",
    "handle_end_session",
    "handle_evolve_canon",
    "handle_keyframe_shot",
    "handle_leave_comment",
    "handle_plan_shot",
    "handle_promote_shot",
    "handle_record_preference",
    "handle_regenerate_shot",
    "handle_register_canon",
    "handle_render_shot",
    "handle_repair_shot",
    "handle_resolve_conflict",
    "handle_score_qa",
    "handle_start_session",
    "handle_swap_reference",
    "handle_switch_mode",
    "handle_update_intent",
]
