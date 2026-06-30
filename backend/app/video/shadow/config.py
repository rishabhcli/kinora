"""Additive settings for the shadow / live-eval harness — safe, off-by-default.

These are read off the central :class:`app.core.config.Settings` (the harness never
re-reads the environment itself), but are *also* expressed as a self-contained
:class:`ShadowSettings` so the package has a typed, defaulted contract that tests
can build directly without constructing the whole ``Settings`` object.

Every default is the safe one:

* ``enabled = False``         — shadow mode is opt-in.
* ``sample_fraction = 0.0``   — even enabled, nothing is sampled until a fraction
  is set.
* ``eval_video_seconds = 0.0`` — the eval budget is unfunded, so a candidate can
  never spend a real video-second (the zero-by-default guard). This is independent
  of, and *in addition to*, the global ``KINORA_LIVE_VIDEO`` gate at the provider.

:func:`shadow_settings_from` adapts a live ``Settings`` (reading optional
``video_shadow_*`` attributes via ``getattr`` so it works whether or not the
central config has been extended) into a :class:`ShadowSettings`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ShadowSettings(BaseModel):
    """Typed, safe-by-default configuration for the shadow harness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Master switch. Off ⇒ the orchestrator should not even construct a runner.
    enabled: bool = False
    #: Fraction of real renders also rendered on the candidate, in ``[0, 1]``.
    sample_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Per-candidate sampling salt (decorrelates concurrent candidate evals).
    sample_salt: str = "shadow"
    #: Funded eval video-seconds. ``0.0`` ⇒ candidate never spends (default).
    eval_video_seconds: float = Field(default=0.0, ge=0.0)
    #: Candidate model id under evaluation (telemetry / report labelling).
    candidate_model: str = ""
    #: Confidence level for every CI in the analysis.
    confidence: float = Field(default=0.95, gt=0.0, lt=1.0)
    #: Dead-band for the win-rate (a quality delta within ±margin is a tie).
    win_margin: float = Field(default=0.0, ge=0.0)

    @property
    def is_live_funded(self) -> bool:
        """True iff an operator explicitly funded candidate spend."""
        return self.eval_video_seconds > 0.0


def shadow_settings_from(settings: Any) -> ShadowSettings:
    """Build :class:`ShadowSettings` from a live ``Settings`` (or any object).

    Reads ``video_shadow_*`` attributes defensively via ``getattr`` so this works
    whether or not the central config has been extended with the additive fields;
    missing attributes fall back to the safe defaults.
    """
    return ShadowSettings(
        enabled=bool(getattr(settings, "video_shadow_enabled", False)),
        sample_fraction=float(getattr(settings, "video_shadow_sample_fraction", 0.0)),
        sample_salt=str(getattr(settings, "video_shadow_sample_salt", "shadow")),
        eval_video_seconds=float(getattr(settings, "video_shadow_eval_video_seconds", 0.0)),
        candidate_model=str(getattr(settings, "video_shadow_candidate_model", "")),
        confidence=float(getattr(settings, "video_shadow_confidence", 0.95)),
        win_margin=float(getattr(settings, "video_shadow_win_margin", 0.0)),
    )


__all__ = ["ShadowSettings", "shadow_settings_from"]
