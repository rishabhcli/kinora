"""The Wan (DashScope) dialect — bit-faithful to ``generator.compose_wan_prompt``.

Wan receives text + media; the designed camera block is folded into the prompt or
the clip defaults to a flat static frame (the §9.2/§9.4 reason the generator does
this at all). This dialect reproduces that fold *exactly* so the canonical
:class:`~app.video.prompts.canonical.ShotDescription` can drive the existing Wan
render path without changing a pixel of output.

The reference structure (see :func:`app.agents.generator.compose_wan_prompt`)::

    "{base}. Camera: {shot}, {speed} {move}. {cinematic_finish}"

where ``base`` is the creative prompt with a trailing "." stripped, the camera
phrase uses the generator's ``_SHOT/_SPEED/_MOVE`` phrasing (here in
:mod:`app.video.prompts.vocab` as ``WAN_*``), and ``cinematic_finish`` is the
generator's :data:`_CINEMATIC_FINISH`. Wan's negative prompt is a separate
channel (the ``WanSpec.negative_prompt`` field), so this dialect emits negatives
in :class:`~app.video.prompts.base.RenderedPrompt.negative_prompt`, not in text.

To reproduce the generator byte-for-byte, build the description with the creative
line as ``action`` and the finish tokens as ``quality_tokens`` (see
:func:`shot_from_wan`), or call :func:`compose_like_generator` directly.
"""

from __future__ import annotations

from ..base import DialectSpec, NegativeStyle, PromptDialect, RenderedPrompt
from ..canonical import CameraDirection, ShotDescription
from ..compress import fit_clauses
from ..vocab import WAN_MOVE, WAN_SHOT, WAN_SPEED, lookup_move, lookup_shot, lookup_speed

#: The generator's exact filmic finish (:data:`app.agents.generator._CINEMATIC_FINISH`).
WAN_CINEMATIC_FINISH = (
    "cinematic composition, volumetric lighting, shallow depth of field, "
    "fluid lifelike motion, atmospheric detail, film grain"
)

#: Wan prompts have no hard documented cap; this is a safe, generous budget.
_WAN_BUDGET = 1800


def wan_camera_phrase(camera: CameraDirection) -> str:
    """The generator's ``_camera_phrase``: ``"{shot}, {speed} {move}"`` for Wan."""
    shot = lookup_shot(camera, WAN_SHOT)
    speed = lookup_speed(camera, WAN_SPEED)
    move = lookup_move(camera, WAN_MOVE)
    return f"{shot}, {speed} {move}".strip()


def compose_like_generator(base_prompt: str, camera: CameraDirection) -> str:
    """Reproduce :func:`app.agents.generator.compose_wan_prompt` exactly.

    ``". ".join`` of: the base prompt (trailing "." stripped), ``"Camera: …"``,
    and the filmic finish — dropping any empty part, identical to the generator.
    """
    base = (base_prompt or "").strip().rstrip(".")
    phrase = wan_camera_phrase(camera)
    parts = [p for p in (base, f"Camera: {phrase}", WAN_CINEMATIC_FINISH) if p]
    return ". ".join(parts)


class WanDialect(PromptDialect):
    """Wan 2.x dialect. Folds the camera block + filmic finish into the text prompt."""

    spec = DialectSpec(
        name="wan",
        label="Wan 2.x (DashScope)",
        prompt_budget=_WAN_BUDGET,
        negative=NegativeStyle(supported=True, budget=512),
        structured=False,
        supports_weighting=False,
        model_ids=("wan2.1-t2v-turbo", "wan2.1-i2v-turbo", "wan2.5-t2v-preview"),
    )

    def render(self, shot: ShotDescription, *, budget: int | None = None) -> RenderedPrompt:
        """Render Wan's text + negative prompt, faithful to the generator's fold.

        The base creative line is the shot's ``action`` joined with ``subject``/
        ``setting``/``mood``/``lighting`` (the same content the generator's
        ``prompt`` carried); the camera + filmic finish are appended exactly as
        the generator does. When the description is the canonical form produced by
        :func:`shot_from_wan`, output is byte-identical to ``compose_wan_prompt``.
        """
        limit = budget if budget is not None else self.spec.prompt_budget
        base = self._base_line(shot)
        prompt = compose_like_generator(base, shot.camera)
        if len(prompt) > limit:
            # Only the rare over-budget case re-fits; the common path is verbatim.
            prompt = fit_clauses(prompt.split(". "), limit)
        negatives = self._negative_terms(shot)
        return RenderedPrompt(
            dialect=self.name,
            prompt=prompt,
            negative_prompt=self._format_negative(negatives) if negatives else None,
        )

    @staticmethod
    def _base_line(shot: ShotDescription) -> str:
        """The creative line: action first, then the descriptive context, comma-joined.

        Mirrors how the Cinematographer's single ``prompt`` string reads — when a
        caller already has that string it is passed as ``action`` alone and the
        others are blank, so the line is exactly the original prompt.
        """
        parts = [
            p.strip()
            for p in (shot.subject, shot.action, shot.setting, shot.mood, shot.lighting)
            if p and p.strip()
        ]
        return ", ".join(parts)

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        # Not used by the overridden render(); provided for the ABC contract and
        # for callers that want the clause view.
        composed = compose_like_generator(self._base_line(shot), shot.camera)
        return composed.split(". ")

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for term in shot.negative_cues:
            key = term.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(term.strip())
        return out


def shot_from_wan(
    *,
    prompt: str,
    camera: CameraDirection,
    negative_prompt: str | None = None,
    seed: int | None = None,
    duration_s: float = 5.0,
) -> ShotDescription:
    """Build the canonical description that reproduces ``compose_wan_prompt(prompt, camera)``.

    The creative ``prompt`` becomes ``action`` (so the Wan dialect's base line is
    exactly it), the filmic finish is implicit (the dialect always appends it),
    and the negative prompt is split back into ``negative_cues``. This is the
    inverse used in tests to prove faithfulness, and the bridge a caller uses to
    lift an existing Wan shot into the canonical layer.
    """
    cues = [t.strip() for t in (negative_prompt or "").split(",") if t.strip()]
    return ShotDescription(
        action=prompt,
        camera=camera,
        negative_cues=cues,
        quality_tokens=[WAN_CINEMATIC_FINISH],
        seed=seed,
        duration_s=duration_s,
    )


__all__ = [
    "WAN_CINEMATIC_FINISH",
    "WanDialect",
    "compose_like_generator",
    "shot_from_wan",
    "wan_camera_phrase",
]
