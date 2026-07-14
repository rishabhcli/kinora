"""Ready-made :class:`CapabilityProfile` s for the backends Kinora knows about.

These are *declarative descriptions*, not live capability probes — a convenience
so callers (and the tests) don't re-spell common envelopes. They intentionally
mirror the real backends documented in ``AGENTS.md`` / ``app.core.config`` without
importing the provider layer (this package stays self-contained):

* :func:`wan_profile` — the hosted DashScope Wan family: every render mode, short
  per-call clips, fixed resolution tiers, seed control.
* :func:`minimax_profile` — hosted MiniMax (Hailuo): t2v + i2v only, no r2v /
  flf / continuation, a discrete duration menu, no seed.
* :func:`text_only_profile` — a deliberately minimal t2v-only backend, the worst
  case the negotiator must still satisfy.

A backend author can start from a preset and ``model_copy(update=…)`` it.
"""

from __future__ import annotations

from .capabilities import (
    ASPECT_1_1,
    ASPECT_4_3,
    ASPECT_9_16,
    ASPECT_16_9,
    ASPECT_21_9,
    CapabilityProfile,
    VideoMode,
)

#: Every Wan render mode (§9.3 decision tree).
_WAN_MODES = frozenset(
    {
        VideoMode.TEXT_TO_VIDEO,
        VideoMode.IMAGE_TO_VIDEO,
        VideoMode.REFERENCE_TO_VIDEO,
        VideoMode.FIRST_LAST_FRAME,
        VideoMode.VIDEO_CONTINUATION,
        VideoMode.INSTRUCTION_EDIT,
    }
)


def wan_profile(name: str = "dashscope-wan") -> CapabilityProfile:
    """Hosted DashScope Wan: full mode set, short clips, seed control."""
    return CapabilityProfile(
        name=name,
        modes=_WAN_MODES,
        min_duration_s=2.0,
        max_duration_s=5.0,
        fps_options=(16, 24),
        resolution_options=("480P", "720P", "1080P"),
        aspect_options=(ASPECT_16_9, ASPECT_9_16, ASPECT_1_1),
        supports_seed=True,
        max_reference_images=4,
        supports_negative_prompt=True,
        max_prompt_chars=1500,
        can_synthesize_keyframe=True,
        supports_continuation_overlap=True,
        overlap_s=0.5,
    )


def minimax_profile(name: str = "minimax-hailuo") -> CapabilityProfile:
    """Hosted MiniMax (Hailuo): t2v + i2v only, discrete durations, no seed."""
    return CapabilityProfile(
        name=name,
        modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
        min_duration_s=6.0,
        max_duration_s=10.0,
        duration_options=(6, 10),
        fps_options=(25,),
        resolution_options=("768P", "1080P"),
        aspect_options=(ASPECT_16_9, ASPECT_9_16, ASPECT_1_1, ASPECT_4_3, ASPECT_21_9),
        supports_seed=False,
        max_reference_images=1,
        supports_negative_prompt=False,
        max_prompt_chars=2000,
        can_synthesize_keyframe=False,
        supports_continuation_overlap=False,
        overlap_s=0.0,
    )


def text_only_profile(name: str = "t2v-only") -> CapabilityProfile:
    """A minimal worst-case backend: text-to-video, fixed format, no seed."""
    return CapabilityProfile(
        name=name,
        modes=frozenset({VideoMode.TEXT_TO_VIDEO}),
        min_duration_s=4.0,
        max_duration_s=4.0,
        fps_options=(24,),
        resolution_options=("720P",),
        aspect_options=(ASPECT_16_9,),
        supports_seed=False,
        max_reference_images=0,
        supports_negative_prompt=False,
        max_prompt_chars=500,
        can_synthesize_keyframe=False,
        supports_continuation_overlap=False,
        overlap_s=0.0,
    )


__all__ = ["minimax_profile", "text_only_profile", "wan_profile"]
