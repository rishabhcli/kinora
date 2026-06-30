"""The generic / open dialect — a neutral baseline for any unlisted model.

A safe, middle-of-the-road translation: a clear comma-joined scene description
with a plain camera clause and a folded "avoid …" tail (no assumed negative
channel). Useful as the registry's fallback for an open-source / unknown model,
and as the conservative default a caller picks when the target model is unknown.
"""

from __future__ import annotations

from ..base import DEFAULT_PROMPT_BUDGET, DialectSpec, NegativeStyle, PromptDialect
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


class GenericDialect(PromptDialect):
    """A neutral, model-agnostic dialect (the registry fallback)."""

    spec = DialectSpec(
        name="generic",
        label="Generic / open model",
        prompt_budget=DEFAULT_PROMPT_BUDGET,
        negative=NegativeStyle(supported=False),
        structured=False,
        supports_weighting=False,
    )

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        lead = subject_action_clause(shot)
        if lead:
            clauses.append(lead)
        if shot.setting.strip():
            clauses.append(shot.setting.strip())
        framing = lookup_shot(shot.camera, RUNWAY_SHOT)
        move = lookup_move(shot.camera, RUNWAY_MOVE)
        speed = lookup_speed(shot.camera, RUNWAY_SPEED)
        cam = ", ".join(b for b in (framing, f"{speed} {move}".strip()) if b)
        angle = lookup_angle(shot.camera)
        if angle:
            cam = f"{cam}, {angle}" if cam else angle
        if cam:
            clauses.append(cam)
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


__all__ = ["GenericDialect"]
