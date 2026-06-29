"""Default structural validators for the command catalog.

These run in the :class:`~app.eventsourcing.domain.middleware.ValidationMiddleware`
*before* the aggregate is even loaded — they catch malformed commands cheaply
(empty ids, out-of-range numbers, unknown enum-like choices) so a bad request
never touches the store. They are deliberately limited to *structural* checks;
*business* invariants (illegal §9.7 transitions, the retry cap, canon-version
staleness) live in the aggregates where they belong.

Each validator raises
:class:`~app.eventsourcing.domain.errors.ValidationError` on a bad command.
"""

from __future__ import annotations

from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.errors import ValidationError
from app.eventsourcing.domain.middleware import ValidationMiddleware


def _require(condition: bool, message: str, *, field: str | None = None) -> None:
    if not condition:
        raise ValidationError(message, field=field)


def validate_start_session(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.StartSession)
    _require(bool(cmd.session_id), "session_id is required", field="session_id")
    _require(bool(cmd.user_id), "user_id is required", field="user_id")
    _require(bool(cmd.book_id), "book_id is required", field="book_id")


def validate_update_intent(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.UpdateIntent)
    _require(bool(cmd.session_id), "session_id is required", field="session_id")
    _require(cmd.focus_word >= 0, "focus_word must be non-negative", field="focus_word")
    _require(cmd.velocity >= 0.0, "velocity must be non-negative", field="velocity")


def validate_leave_comment(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.LeaveDirectorComment)
    _require(bool(cmd.session_id), "session_id is required", field="session_id")
    _require(bool(cmd.shot_id), "shot_id is required", field="shot_id")
    _require(bool(cmd.note.strip()), "note must not be empty", field="note")
    if cmd.region is not None:
        _require(len(cmd.region) == 4, "region must be a 4-tuple", field="region")


def validate_plan_shot(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.PlanShot)
    _require(bool(cmd.shot_id), "shot_id is required", field="shot_id")
    _require(bool(cmd.shot_hash), "shot_hash is required (§8.7 dedup key)", field="shot_hash")


def validate_render_shot(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.RenderShot)
    _require(bool(cmd.shot_id), "shot_id is required", field="shot_id")
    _require(bool(cmd.shot_hash), "shot_hash is required (idempotency key)", field="shot_hash")
    _require(cmd.video_seconds >= 0.0, "video_seconds must be non-negative", field="video_seconds")
    if cmd.cache_hit:
        _require(bool(cmd.clip_url), "a cache hit must carry a clip_url", field="clip_url")


def validate_score_qa(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.ScoreShotQA)
    _require(bool(cmd.shot_id), "shot_id is required", field="shot_id")
    _require(0.0 <= cmd.score <= 1.0, "score must be within [0, 1]", field="score")


def validate_resolve_conflict(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.ResolveShotConflict)
    _require(bool(cmd.shot_id), "shot_id is required", field="shot_id")
    _require(
        cmd.decision in {"regen", "accept", "degrade"},
        "decision must be regen|accept|degrade",
        field="decision",
    )


def validate_register_canon(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.RegisterCanonEntity)
    _require(bool(cmd.entity_id), "entity_id is required", field="entity_id")
    _require(bool(cmd.book_id), "book_id is required", field="book_id")
    _require(bool(cmd.name.strip()), "name is required", field="name")


def validate_edit_canon(command: Command) -> None:
    cmd = command
    assert isinstance(cmd, cc.EditCanonField)
    _require(bool(cmd.entity_id), "entity_id is required", field="entity_id")
    _require(bool(cmd.field_name), "field_name is required", field="field_name")
    if cmd.expected_canon_version is not None:
        _require(
            cmd.expected_canon_version >= 0,
            "expected_canon_version must be non-negative",
            field="expected_canon_version",
        )


def register_default_validators(middleware: ValidationMiddleware) -> ValidationMiddleware:
    """Register every default structural validator onto ``middleware``."""
    middleware.register(cc.StartSession.command_type, validate_start_session)
    middleware.register(cc.UpdateIntent.command_type, validate_update_intent)
    middleware.register(cc.LeaveDirectorComment.command_type, validate_leave_comment)
    middleware.register(cc.PlanShot.command_type, validate_plan_shot)
    middleware.register(cc.RenderShot.command_type, validate_render_shot)
    middleware.register(cc.ScoreShotQA.command_type, validate_score_qa)
    middleware.register(cc.ResolveShotConflict.command_type, validate_resolve_conflict)
    middleware.register(cc.RegisterCanonEntity.command_type, validate_register_canon)
    middleware.register(cc.EditCanonField.command_type, validate_edit_canon)
    return middleware


__all__ = [
    "register_default_validators",
    "validate_edit_canon",
    "validate_leave_comment",
    "validate_plan_shot",
    "validate_register_canon",
    "validate_render_shot",
    "validate_resolve_conflict",
    "validate_score_qa",
    "validate_start_session",
    "validate_update_intent",
]
