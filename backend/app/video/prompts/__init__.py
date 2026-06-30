"""Prompt-dialect translation layer — one canonical shot, many model prompts.

Different video models want different prompt phrasing: a different camera grammar,
negative-prompt support and format, style/quality tokens, length budget,
structured-vs-free-text, weighting syntax. Today the Cinematographer's design is
compiled only for Wan (:func:`app.agents.generator.compose_wan_prompt`). This
package decouples *intent* from *phrasing*:

* :class:`ShotDescription` — the canonical, model-agnostic shot (subject, action,
  setting, mood, camera move/framing/speed, lighting, style refs, continuity
  tags, negative cues).
* :class:`PromptDialect` — the plugin interface; one per model. Concrete dialects:
  Wan (bit-faithful to today's output), Runway, Pika, Kling, Luma, Veo, Sora, and
  a generic/open baseline.
* :func:`fit_clauses` — a length-aware compressor every dialect uses so a prompt
  is always non-empty and within the model's budget.
* :class:`DialectRegistry` / :func:`render_for` — look a dialect up by model name
  (alias-aware) and translate.

Everything here is pure and deterministic; importing this package is side-effect
free (the default registry is built lazily on first use).

Example::

    from app.video.prompts import ShotDescription, CameraDirection, CameraMove, render_for

    shot = ShotDescription(
        subject="a lone knight",
        action="rides across a misted moor",
        setting="a grey dawn moor",
        mood="ominous",
        camera=CameraDirection(move=CameraMove.PUSH_IN),
        negative_cues=["blurry", "extra fingers"],
    )
    wan = render_for("wan2.1-t2v-turbo", shot)      # → RenderedPrompt for Wan
    veo = render_for("veo-3", shot)                 # → richer prose for Veo
"""

from __future__ import annotations

from .base import (
    DEFAULT_PROMPT_BUDGET,
    DialectSpec,
    NegativeStyle,
    PromptDialect,
    RenderedPrompt,
)
from .canonical import (
    CameraAngle,
    CameraDirection,
    CameraMove,
    CameraSpeed,
    RenderIntent,
    ShotDescription,
    ShotSize,
    coerce_move,
    coerce_shot_size,
    coerce_speed,
)
from .compress import fit_clauses, join_within, shorten_text
from .registry import (
    FALLBACK_DIALECT,
    DialectRegistry,
    build_default_registry,
    default_registry,
    get_dialect,
    render_for,
)

__all__ = [
    "DEFAULT_PROMPT_BUDGET",
    "FALLBACK_DIALECT",
    "CameraAngle",
    "CameraDirection",
    "CameraMove",
    "CameraSpeed",
    "DialectRegistry",
    "DialectSpec",
    "NegativeStyle",
    "PromptDialect",
    "RenderIntent",
    "RenderedPrompt",
    "ShotDescription",
    "ShotSize",
    "build_default_registry",
    "coerce_move",
    "coerce_shot_size",
    "coerce_speed",
    "default_registry",
    "fit_clauses",
    "get_dialect",
    "join_within",
    "render_for",
    "shorten_text",
]
