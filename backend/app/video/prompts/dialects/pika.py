"""The Pika dialect — a very short prompt + ``-camera``/``-neg`` parameters.

Pika famously rewards *short* prompts and exposes camera control and a negative
prompt as trailing ``-parameter`` flags (e.g. ``-camera zoom in``, ``-neg blurry``)
rather than a separate API field. This dialect therefore:

* keeps the positive prompt tight (a small char budget), leading with subject +
  action and only the most load-bearing context, and
* renders the camera move and the negative cues as ``-camera``/``-neg`` flags
  *inside* the prompt string (Pika has no separate negative channel — so the base
  reports ``negative.supported = False`` and the flags ride the text).
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect, RenderedPrompt
from ..canonical import CameraMove, ShotDescription
from ..compress import fit_clauses, shorten_text
from ._shared import negative_terms, subject_action_clause

#: Pika prompts are short by design.
_PIKA_BUDGET = 280

#: Pika's ``-camera`` flag vocabulary (the documented motion keywords).
_PIKA_CAMERA: dict[CameraMove, str] = {
    CameraMove.PUSH_IN: "zoom in",
    CameraMove.ZOOM: "zoom in",
    CameraMove.ZOOM_IN: "zoom in",
    CameraMove.PULL_OUT: "zoom out",
    CameraMove.ZOOM_OUT: "zoom out",
    CameraMove.PAN_LEFT: "pan left",
    CameraMove.PAN_RIGHT: "pan right",
    CameraMove.TILT_UP: "tilt up",
    CameraMove.TILT_DOWN: "tilt down",
    # Pika has no dedicated track/orbit/crane flag; these map to the closest.
    CameraMove.TRACK: "pan right",
    CameraMove.FOLLOW: "pan right",
    CameraMove.ORBIT: "rotate",
    CameraMove.CRANE_UP: "tilt up",
    CameraMove.CRANE_DOWN: "tilt down",
    # STATIC / HANDHELD: no flag (Pika holds by default).
}


class PikaDialect(PromptDialect):
    """Pika dialect. Short prompt + ``-camera`` / ``-neg`` trailing flags."""

    spec = DialectSpec(
        name="pika",
        label="Pika",
        prompt_budget=_PIKA_BUDGET,
        negative=NegativeStyle(supported=False),
        structured=False,
        supports_weighting=False,
        model_ids=("pika-1.5", "pika-2.0"),
    )

    def render(self, shot: ShotDescription, *, budget: int | None = None) -> RenderedPrompt:
        """Fit the short scene text, then append the ``-camera``/``-neg`` flags.

        Flags are reserved out of the budget first (they are near-non-negotiable
        model control), then the scene description fills the remainder; this keeps
        the whole string — text + flags — within the cap. If the budget is so
        tight that the flags alone overflow it, the final string is truncated so
        the budget guarantee still holds (an absurd budget is the only case that
        sacrifices a flag).
        """
        limit = budget if budget is not None else self.spec.prompt_budget
        flags = self._flags(shot)
        flag_str = " " + " ".join(flags) if flags else ""
        text_budget = max(0, limit - len(flag_str))
        text = fit_clauses(self._compose_clauses(shot), text_budget)
        prompt = (text + flag_str).strip()
        if len(prompt) > limit:
            prompt = shorten_text(prompt, limit)
        return RenderedPrompt(dialect=self.name, prompt=prompt)

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        clauses: list[str] = []
        lead = subject_action_clause(shot)
        if lead:
            clauses.append(lead)
        # Pika is terse: only the single most useful context line each.
        if shot.setting.strip():
            clauses.append(shot.setting.strip())
        if shot.mood.strip():
            clauses.append(shot.mood.strip())
        if shot.style_refs:
            clauses.append(", ".join(shot.style_refs))
        return clauses

    def _flags(self, shot: ShotDescription) -> list[str]:
        flags: list[str] = []
        move = shot.camera.move
        if isinstance(move, CameraMove):
            keyword = _PIKA_CAMERA.get(move)
            if keyword:
                flags.append(f"-camera {keyword}")
        negatives = negative_terms(shot)
        if negatives:
            flags.append("-neg " + " ".join(negatives[:6]))
        return flags

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return negative_terms(shot)


__all__ = ["PikaDialect"]
