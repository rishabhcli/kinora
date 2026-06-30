"""The Kling dialect — natural cinematic prose + a dedicated negative prompt.

Kling reads a moderate-length natural-language prompt and has a real
negative-prompt field, so this dialect emits negatives in the native channel (not
folded into the text). Camera motion is phrased as a natural clause ("the camera
pushes in slowly").
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect
from ..canonical import ShotDescription
from ..vocab import (
    KLING_MOVE,
    KLING_SHOT,
    KLING_SPEED,
    lookup_angle,
    lookup_move,
    lookup_shot,
    lookup_speed,
)
from ._shared import negative_terms, subject_action_clause


class KlingDialect(PromptDialect):
    """Kling dialect. Natural cinematic prose + native negative prompt."""

    spec = DialectSpec(
        name="kling",
        label="Kling",
        prompt_budget=2500,
        negative=NegativeStyle(supported=True, budget=2500),
        structured=False,
        supports_weighting=False,
        model_ids=("kling-v1", "kling-v1.5", "kling-v2"),
    )

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        lead = subject_action_clause(shot)
        if lead:
            clauses.append(lead)
        if shot.setting.strip():
            clauses.append(shot.setting.strip())
        # Camera as a natural clause: "wide shot, the camera pushes in smoothly".
        framing = lookup_shot(shot.camera, KLING_SHOT)
        move = lookup_move(shot.camera, KLING_MOVE)
        speed = lookup_speed(shot.camera, KLING_SPEED)
        cam = ", ".join(b for b in (framing, f"{move} {speed}".strip()) if b)
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


__all__ = ["KlingDialect"]
