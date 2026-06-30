"""The Luma Dream Machine dialect — flowing natural-language motion, no negatives.

Luma reads a single descriptive prompt and responds best to natural prose that
narrates the motion ("the camera glides in as …"). It has no negative-prompt
input, so avoid-cues fold into a short trailing "without …" clause.
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect
from ..canonical import ShotDescription
from ..vocab import (
    LUMA_MOVE,
    LUMA_SHOT,
    LUMA_SPEED,
    lookup_angle,
    lookup_move,
    lookup_shot,
    lookup_speed,
)
from ._shared import negative_terms, subject_action_clause


class LumaDialect(PromptDialect):
    """Luma Dream Machine dialect. Flowing prose; no native negative prompt."""

    spec = DialectSpec(
        name="luma",
        label="Luma Dream Machine",
        prompt_budget=1200,
        negative=NegativeStyle(supported=False),
        structured=False,
        supports_weighting=False,
        model_ids=("ray-1.6", "ray-2"),
    )

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        # Luma's lead reads as a framed scene: "an intimate close-up: <subject action>".
        framing = lookup_shot(shot.camera, LUMA_SHOT)
        lead = subject_action_clause(shot)
        if framing and lead:
            clauses.append(f"{framing}: {lead}")
        elif lead:
            clauses.append(lead)
        elif framing:
            clauses.append(framing)
        if shot.setting.strip():
            clauses.append(f"set in {shot.setting.strip()}")
        # Motion narrated as prose: "the camera glides in slowly".
        move = lookup_move(shot.camera, LUMA_MOVE)
        speed = lookup_speed(shot.camera, LUMA_SPEED)
        if move:
            clauses.append(f"{move} {speed}".strip())
        angle = lookup_angle(shot.camera)
        if angle:
            clauses.append(f"shot from a {angle}")
        if shot.lighting.strip():
            clauses.append(shot.lighting.strip())
        if shot.mood.strip():
            clauses.append(f"the mood is {shot.mood.strip()}")
        if shot.style_refs:
            clauses.append(", ".join(shot.style_refs))
        if shot.quality_tokens:
            clauses.append(", ".join(shot.quality_tokens))
        return clauses

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return negative_terms(shot)

    def _fold_negative_clause(self, terms: list[str]) -> str:
        head = ", ".join(terms[:6])
        return f"without {head}" if head else ""


__all__ = ["LumaDialect"]
