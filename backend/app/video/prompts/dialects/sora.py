"""The OpenAI Sora dialect — long descriptive prose, no negative-prompt channel.

Sora takes a single long natural-language description and has no negative-prompt
input; the documented guidance is to *describe what you want*, not what to avoid,
so this dialect leads with a vivid scene sentence and folds only a brief
"avoiding …" tail when negative cues exist (dropped first under budget).
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect
from ..canonical import ShotDescription
from ..vocab import (
    PROSE_MOVE,
    PROSE_SHOT,
    PROSE_SPEED,
    lookup_angle,
    lookup_move,
    lookup_shot,
    lookup_speed,
)
from ._shared import negative_terms, subject_action_clause


class SoraDialect(PromptDialect):
    """OpenAI Sora dialect. Long descriptive prose; no native negative prompt."""

    spec = DialectSpec(
        name="sora",
        label="OpenAI Sora",
        prompt_budget=4000,
        negative=NegativeStyle(supported=False),
        structured=False,
        supports_weighting=False,
        model_ids=("sora-1", "sora-turbo"),
    )

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        framing = lookup_shot(shot.camera, PROSE_SHOT)
        lead = subject_action_clause(shot)
        if framing and lead:
            clauses.append(f"{framing} of {lead}")
        elif lead:
            clauses.append(lead)
        elif framing:
            clauses.append(framing)
        if shot.setting.strip():
            clauses.append(f"set in {shot.setting.strip()}")
        move = lookup_move(shot.camera, PROSE_MOVE)
        speed = lookup_speed(shot.camera, PROSE_SPEED)
        if move:
            cam = f"{move}, {speed}".strip(", ").strip()
            angle = lookup_angle(shot.camera)
            if angle:
                cam = f"{cam}, from a {angle}"
            clauses.append(f"captured with {cam}")
        if shot.lighting.strip():
            clauses.append(f"lit by {shot.lighting.strip()}")
        if shot.mood.strip():
            clauses.append(f"evoking a {shot.mood.strip()} mood")
        if shot.style_refs:
            clauses.append("rendered in the style of " + ", ".join(shot.style_refs))
        if shot.quality_tokens:
            clauses.append(", ".join(shot.quality_tokens))
        return clauses

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return negative_terms(shot)

    def _fold_negative_clause(self, terms: list[str]) -> str:
        head = ", ".join(terms[:6])
        return f"avoiding {head}" if head else ""


__all__ = ["SoraDialect"]
