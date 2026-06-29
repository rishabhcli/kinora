"""The concrete command catalog for the three aggregates.

Each command is a frozen dataclass carrying the target aggregate id and the data
its decision needs. They are grouped by aggregate (session / render-shot / canon)
and each implements :meth:`~app.eventsourcing.domain.commands.Command.target_stream`.

Commands that the §12.1 idempotency guarantee applies to (notably ``RenderShot``,
keyed on ``shot_hash``) expose an ``idempotency_key`` so the bus dedupes retried
submissions — "re-enqueuing the same shot is a no-op".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.enums import EntityType, SessionMode
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.identifiers import StreamId

# --------------------------------------------------------------------------- #
# Session commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StartSession(Command):
    session_id: str = ""
    user_id: str = ""
    book_id: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)


@dataclass(frozen=True, slots=True)
class UpdateIntent(Command):
    session_id: str = ""
    focus_word: int = 0
    velocity: float = 0.0

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)


@dataclass(frozen=True, slots=True)
class SwitchMode(Command):
    session_id: str = ""
    mode: SessionMode = SessionMode.VIEWER

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)


@dataclass(frozen=True, slots=True)
class LeaveDirectorComment(Command):
    session_id: str = ""
    comment_id: str = ""
    shot_id: str = ""
    note: str = ""
    routed_agent: str = ""
    region: tuple[float, float, float, float] | None = None

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)

    @property
    def idempotency_key(self) -> str | None:
        # Dedupe a double-submitted comment by its client-assigned id.
        return self.comment_id or None


@dataclass(frozen=True, slots=True)
class RecordPreference(Command):
    session_id: str = ""
    key: str = ""
    value: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)


@dataclass(frozen=True, slots=True)
class EndSession(Command):
    session_id: str = ""
    reason: str = "closed"

    def target_stream(self) -> StreamId:
        return StreamId.session(self.session_id)


# --------------------------------------------------------------------------- #
# Render-shot commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanShot(Command):
    shot_id: str = ""
    book_id: str = ""
    scene_id: str = ""
    beat_id: str = ""
    shot_hash: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class KeyframeShot(Command):
    shot_id: str = ""
    keyframe_url: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class PromoteShot(Command):
    shot_id: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class RenderShot(Command):
    """Drive the CacheCheck -> (Accepted|Rendering->QA) leg of §9.7.

    Idempotent on ``shot_hash`` (§12.1): a duplicate render request for the same
    shot is a no-op that returns the prior result, so duplicate Scheduler events
    can never double-spend the video budget.
    """

    shot_id: str = ""
    shot_hash: str = ""
    cache_hit: bool = False
    clip_url: str = ""
    video_seconds: float = 0.0

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)

    @property
    def idempotency_key(self) -> str | None:
        return self.shot_hash or None


@dataclass(frozen=True, slots=True)
class ScoreShotQA(Command):
    shot_id: str = ""
    score: float = 0.0
    passed: bool = False
    reason: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class RepairShot(Command):
    """QA-fail -> Repair -> (regen | conflict | degrade), per the §9.7 retry policy."""

    shot_id: str = ""
    contradiction_state_id: str = ""
    conflict_detail: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class ResolveShotConflict(Command):
    shot_id: str = ""
    decision: str = "regen"  # one of: regen | accept | degrade

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


@dataclass(frozen=True, slots=True)
class RegenerateShot(Command):
    """§5.4 surgical re-do: re-open a settled/in-flight shot to ``Promoted``.

    Issued by the Director-comment / canon-edit sagas, not by the §9.7 QA loop.
    A no-op when the shot is already back in the render flow.
    """

    shot_id: str = ""
    reason: str = ""
    triggered_by: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.render_shot(self.shot_id)


# --------------------------------------------------------------------------- #
# Canon commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegisterCanonEntity(Command):
    entity_id: str = ""
    book_id: str = ""
    entity_type: EntityType = EntityType.CHARACTER
    name: str = ""

    def target_stream(self) -> StreamId:
        return StreamId.canon(self.entity_id)


@dataclass(frozen=True, slots=True)
class EditCanonField(Command):
    entity_id: str = ""
    field_name: str = ""
    new_value: str = ""
    dependent_shot_ids: tuple[str, ...] = ()
    expected_canon_version: int | None = None

    def target_stream(self) -> StreamId:
        return StreamId.canon(self.entity_id)


@dataclass(frozen=True, slots=True)
class SwapReferenceImage(Command):
    entity_id: str = ""
    new_reference_id: str = ""
    dependent_shot_ids: tuple[str, ...] = ()
    expected_canon_version: int | None = None

    def target_stream(self) -> StreamId:
        return StreamId.canon(self.entity_id)


@dataclass(frozen=True, slots=True)
class EvolveCanonFromConflict(Command):
    entity_id: str = ""
    field_name: str = ""
    new_value: str = ""
    conflict_id: str = ""
    dependent_shot_ids: tuple[str, ...] = ()

    def target_stream(self) -> StreamId:
        return StreamId.canon(self.entity_id)


__all__ = [
    "EditCanonField",
    "EndSession",
    "EvolveCanonFromConflict",
    "KeyframeShot",
    "LeaveDirectorComment",
    "PlanShot",
    "PromoteShot",
    "RecordPreference",
    "RegenerateShot",
    "RegisterCanonEntity",
    "RenderShot",
    "RepairShot",
    "ResolveShotConflict",
    "ScoreShotQA",
    "StartSession",
    "SwapReferenceImage",
    "SwitchMode",
    "UpdateIntent",
]
