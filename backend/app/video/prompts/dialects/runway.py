"""The Runway Gen-3 dialect — concise prose + a keyed camera clause, no negatives.

Runway Gen-3 reads a single free-text prompt and responds well to an explicit,
terse camera instruction ("camera: dolly in") appended to a vivid scene
description. It has no dedicated negative-prompt input, so avoid-cues are folded
into a short trailing "no …" clause (dropped first under a tight budget).
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect
from ..canonical import ShotDescription
from ..vocab import (
    RUNWAY_MOVE,
    RUNWAY_SHOT,
    RUNWAY_SPEED,
    lookup_angle,
    lookup_move,
    lookup_shot,
    lookup_speed,
)
from ._shared import negative_terms, subject_action_clause

#: Runway Gen-3 keeps prompts focused; a mid-length budget.
_RUNWAY_BUDGET = 900


class RunwayDialect(PromptDialect):
    """Runway Gen-3 Alpha dialect."""

    spec = DialectSpec(
        name="runway",
        label="Runway Gen-3 Alpha",
        prompt_budget=_RUNWAY_BUDGET,
        negative=NegativeStyle(supported=False),
        structured=False,
        supports_weighting=False,
        model_ids=("gen3a_turbo", "gen3a"),
    )

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        lead = subject_action_clause(shot)
        if lead:
            clauses.append(lead)
        if shot.setting.strip():
            clauses.append(shot.setting.strip())
        # A terse keyed camera clause is Runway's idiom: "wide shot, slow dolly in".
        framing = lookup_shot(shot.camera, RUNWAY_SHOT)
        move = lookup_move(shot.camera, RUNWAY_MOVE)
        speed = lookup_speed(shot.camera, RUNWAY_SPEED)
        cam_bits = [b for b in (framing, f"{speed} {move}".strip()) if b]
        angle = lookup_angle(shot.camera)
        if angle:
            cam_bits.append(angle)
        if cam_bits:
            clauses.append("camera: " + ", ".join(cam_bits))
        if shot.lighting.strip():
            clauses.append(shot.lighting.strip())
        if shot.mood.strip():
            clauses.append(shot.mood.strip())
        if shot.style_refs:
            clauses.append(", ".join(shot.style_refs))
        if shot.quality_tokens:
            clauses.append(", ".join(shot.quality_tokens))
        return clauses

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return negative_terms(shot)

    def _fold_negative_clause(self, terms: list[str]) -> str:
        head = ", ".join(terms[:6])
        return f"no {head}" if head else ""


__all__ = ["RunwayDialect"]
