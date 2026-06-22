"""Continuity Supervisor — guards the canon, raises structured conflicts (§7.2, §8.5).

Given a proposed shot and the active continuity facts at the current beat, it
asks a reasoning model whether the depiction contradicts the canon (e.g. a shot
draws a sword that was retired at an earlier beat). The *judgment* is a model
call; the *conflict construction* is deterministic and typed — a well-formed
:class:`ConflictObject` (§7.2) with the fixed honor/surface/evolve options that
the Showrunner then arbitrates.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonSlice, StateSlice
from app.providers import Providers

from .base import BaseAgent
from .contracts import (
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    ConflictType,
    ContinuityResult,
    ShotSpec,
)
from .prompts import CONTINUITY


class ContinuityJudgment(BaseModel):
    """The reasoning model's raw verdict on a proposed depiction (internal)."""

    model_config = ConfigDict(extra="ignore")

    contradicts: bool = False
    contradicting_state_id: str | None = None
    claim: str = ""
    canon_fact: str | None = None
    reasoning: str = ""


def build_conflict(
    judgment: ContinuityJudgment,
    *,
    shot_id: str | None,
    current_beat: str | None,
    active_states: list[StateSlice],
    target_duration_s: float = 5.0,
) -> ConflictObject:
    """Construct the §7.2 conflict object from a contradiction judgment (deterministic)."""
    cited = next(
        (s for s in active_states if s.state_id == judgment.contradicting_state_id), None
    )
    canon_fact = judgment.canon_fact or (format_state(cited) if cited else None)
    options = [
        ConflictOptionSpec(
            id=ConflictOption.HONOR_CANON,
            action="regenerate the shot honouring the established canon",
            cost_video_s=target_duration_s,
        ),
        ConflictOptionSpec(
            id=ConflictOption.SURFACE_TO_USER,
            action="ask the director to choose",
            cost_video_s=0.0,
        ),
        ConflictOptionSpec(
            id=ConflictOption.EVOLVE_CANON,
            action="assert the new state and regenerate",
            requires="textual support",
        ),
    ]
    return ConflictObject(
        conflict_id=f"cf_{shot_id or current_beat or 'unknown'}",
        raised_by="continuity_supervisor",
        type=ConflictType.CANON_VIOLATION,
        shot_id=shot_id,
        claim=judgment.claim or "the proposed shot contradicts the established canon",
        canon_fact=canon_fact,
        current_beat=current_beat,
        contradicting_state_id=judgment.contradicting_state_id,
        user_facing=True,
        options=options,
    )


def format_state(state: StateSlice) -> str:
    """Render an active continuity fact as a human-readable canon fact string."""
    interval = f"valid from beat {state.valid_from_beat}"
    if state.valid_to_beat is not None:
        interval += f" to {state.valid_to_beat}"
    return (
        f"{state.state_id}: {state.subject_entity_key} {state.predicate} "
        f"{state.object_value} ({interval})"
    )


class Continuity(BaseAgent):
    """Detects canon violations in a proposed shot and raises a typed conflict."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="continuity_supervisor",
            model=settings.chat_model_plus,
            prompt=CONTINUITY,
            skills=skills,  # type: ignore[arg-type]
        )

    async def check_shot(
        self,
        proposed: ShotSpec | str,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> ContinuityResult:
        """Return a clean result, or a structured conflict on a canon violation."""
        depiction, resolved_shot_id = self._depiction(proposed, shot_id)
        current_beat = current_beat_id or canon_slice.beat_id
        payload = {
            "proposed_depiction": depiction,
            "current_beat": current_beat,
            "active_states": [s.model_dump(mode="json") for s in canon_slice.active_states],
        }
        judgment = await self.run_json(payload, ContinuityJudgment, temperature=0.0)
        if not judgment.contradicts:
            return ContinuityResult(ok=True, conflict=None)
        conflict = build_conflict(
            judgment,
            shot_id=resolved_shot_id,
            current_beat=current_beat,
            active_states=list(canon_slice.active_states),
            target_duration_s=target_duration_s,
        )
        return ContinuityResult(ok=False, conflict=conflict)

    @staticmethod
    def _depiction(proposed: ShotSpec | str, shot_id: str | None) -> tuple[str, str | None]:
        if isinstance(proposed, ShotSpec):
            text = proposed.prompt or (proposed.beat_id or "")
            return text, proposed.shot_id or shot_id
        return proposed, shot_id


__all__ = ["Continuity", "ContinuityJudgment", "build_conflict", "format_state"]
