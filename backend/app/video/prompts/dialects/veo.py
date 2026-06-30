"""The Google Veo dialect — long cinematic paragraph prose + a negative prompt.

Veo rewards rich, descriptive paragraphs that read like a cinematographer's note:
the subject and action first, then the setting, the camera as a full prose phrase
("a slow dolly push-in"), the lighting, the mood, and the look. Veo exposes a
negative-prompt input, so avoid-cues use the native channel.
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


class VeoDialect(PromptDialect):
    """Google Veo dialect. Long cinematic prose + native negative prompt."""

    spec = DialectSpec(
        name="veo",
        label="Google Veo",
        prompt_budget=4000,
        negative=NegativeStyle(supported=True, budget=1000),
        structured=False,
        supports_weighting=False,
        model_ids=("veo-2", "veo-3"),
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
            clauses.append(f"the scene is set in {shot.setting.strip()}")
        move = lookup_move(shot.camera, PROSE_MOVE)
        speed = lookup_speed(shot.camera, PROSE_SPEED)
        if move:
            cam = f"{move}, {speed}".strip(", ").strip()
            angle = lookup_angle(shot.camera)
            if angle:
                cam = f"{cam}, from a {angle}"
            clauses.append(f"the camera performs {cam}")
        if shot.lighting.strip():
            clauses.append(f"lit with {shot.lighting.strip()}")
        if shot.mood.strip():
            clauses.append(f"the mood is {shot.mood.strip()}")
        if shot.style_refs:
            clauses.append("in the style of " + ", ".join(shot.style_refs))
        if shot.quality_tokens:
            clauses.append(", ".join(shot.quality_tokens))
        return clauses

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return negative_terms(shot)


__all__ = ["VeoDialect"]
