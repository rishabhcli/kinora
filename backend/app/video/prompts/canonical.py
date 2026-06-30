"""The model-agnostic :class:`ShotDescription` — Kinora's canonical shot prompt.

Every video model (Wan, Runway, Pika, Kling, Luma, Veo, Sora, …) wants its
*prompt* phrased differently: a different camera vocabulary, a different way of
expressing (or not supporting) a negative prompt, different style/quality tokens,
a different length budget, structured-vs-free-text, weighting syntax. Today the
Cinematographer's design is compiled straight into a Wan prompt by
:func:`app.agents.generator.compose_wan_prompt`, which folds the
:class:`~app.agents.contracts.Camera` block into Wan-specific film-grammar.

This module breaks that coupling. A :class:`ShotDescription` is the *intent* — the
subject, action, setting, mood, camera move/framing/speed, lighting, style refs,
continuity tags, and negative cues — decoupled from any one model's phrasing. The
:mod:`app.video.prompts.dialects` then translate one canonical description into
each model's best prompt (within its length budget). The Wan dialect is bit-faithful
to ``compose_wan_prompt`` so nothing in the current render path changes.

Pure data + small enums; no I/O, no network, no provider imports. Pydantic v2.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Camera grammar — a canonical, model-neutral vocabulary
#
# A dialect maps these enums onto its own film-grammar phrasing. Keeping the
# canonical vocabulary *closed* (an enum, not free text) is what lets every
# dialect translate faithfully: a ``CameraMove.DOLLY_IN`` becomes Wan's "slow
# dolly push-in", Runway's structured ``camera: push in``, Pika's ``-camera
# zoom in`` parameter, etc. Free-text moves are still accepted (see
# :class:`CameraDirection.raw_*`) and pass through verbatim when no enum fits.
# --------------------------------------------------------------------------- #


class CameraMove(StrEnum):
    """A canonical camera move. Dialects render each into their own phrasing."""

    STATIC = "static"
    PUSH_IN = "push_in"  # dolly toward the subject
    PULL_OUT = "pull_out"  # dolly away from the subject
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    TILT_UP = "tilt_up"
    TILT_DOWN = "tilt_down"
    TRACK = "track"  # travel alongside the action
    FOLLOW = "follow"  # travel behind/with the subject
    ORBIT = "orbit"  # arc around the subject
    CRANE_UP = "crane_up"
    CRANE_DOWN = "crane_down"
    HANDHELD = "handheld"
    ZOOM = "zoom"  # an undirected optical zoom (direction unspecified)
    ZOOM_IN = "zoom_in"  # optical zoom in (distinct from a physical dolly)
    ZOOM_OUT = "zoom_out"


class ShotSize(StrEnum):
    """A canonical framing / shot size."""

    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    FULL = "full"
    MEDIUM = "medium"
    COWBOY = "cowboy"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"


class CameraSpeed(StrEnum):
    """How fast a move executes — the rhythm of the camera."""

    SLOW = "slow"
    MEDIUM = "medium"
    FAST = "fast"


class CameraAngle(StrEnum):
    """The vertical angle the camera looks from (optional; ``EYE_LEVEL`` is neutral)."""

    EYE_LEVEL = "eye_level"
    LOW_ANGLE = "low_angle"
    HIGH_ANGLE = "high_angle"
    BIRDS_EYE = "birds_eye"
    DUTCH = "dutch"  # canted / tilted horizon


#: Sentinel for an alias map miss — distinguishes "no canonical match" from any
#: real enum member, so ``coerce_*`` helpers can fall back to free text.
_UNMATCHED = object()


def _norm(value: str) -> str:
    """Normalize a free-text camera token the way :func:`generator._norm` does."""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


#: Free-text → canonical move aliases. A superset of the generator's ``_MOVE_PHRASES``
#: keys so an existing :class:`~app.agents.contracts.Camera` coerces losslessly.
_MOVE_ALIASES: dict[str, CameraMove] = {
    "static": CameraMove.STATIC,
    "locked": CameraMove.STATIC,
    "locked_off": CameraMove.STATIC,
    "push": CameraMove.PUSH_IN,
    "push_in": CameraMove.PUSH_IN,
    "dolly_in": CameraMove.PUSH_IN,
    "pull": CameraMove.PULL_OUT,
    "pull_out": CameraMove.PULL_OUT,
    "dolly_out": CameraMove.PULL_OUT,
    "pan": CameraMove.PAN_RIGHT,
    "pan_left": CameraMove.PAN_LEFT,
    "pan_right": CameraMove.PAN_RIGHT,
    "tilt": CameraMove.TILT_UP,
    "tilt_up": CameraMove.TILT_UP,
    "tilt_down": CameraMove.TILT_DOWN,
    "track": CameraMove.TRACK,
    "tracking": CameraMove.TRACK,
    "follow": CameraMove.FOLLOW,
    "orbit": CameraMove.ORBIT,
    "arc": CameraMove.ORBIT,
    "crane": CameraMove.CRANE_UP,
    "crane_up": CameraMove.CRANE_UP,
    "crane_down": CameraMove.CRANE_DOWN,
    "handheld": CameraMove.HANDHELD,
    "zoom": CameraMove.ZOOM,
    "zoom_in": CameraMove.ZOOM_IN,
    "zoom_out": CameraMove.ZOOM_OUT,
}
_SHOT_ALIASES: dict[str, ShotSize] = {
    "extreme_wide": ShotSize.EXTREME_WIDE,
    "wide": ShotSize.WIDE,
    "establishing": ShotSize.WIDE,
    "full": ShotSize.FULL,
    "medium": ShotSize.MEDIUM,
    "cowboy": ShotSize.COWBOY,
    "close": ShotSize.CLOSE_UP,
    "closeup": ShotSize.CLOSE_UP,
    "close_up": ShotSize.CLOSE_UP,
    "extreme_close": ShotSize.EXTREME_CLOSE_UP,
    "extreme_close_up": ShotSize.EXTREME_CLOSE_UP,
}
_SPEED_ALIASES: dict[str, CameraSpeed] = {
    "slow": CameraSpeed.SLOW,
    "medium": CameraSpeed.MEDIUM,
    "steady": CameraSpeed.MEDIUM,
    "fast": CameraSpeed.FAST,
}


def coerce_move(value: str) -> CameraMove | str:
    """Map a free-text move onto :class:`CameraMove`, else return the cleaned text."""
    match = _MOVE_ALIASES.get(_norm(value), _UNMATCHED)
    return match if match is not _UNMATCHED else value.strip()  # type: ignore[return-value]


def coerce_shot_size(value: str) -> ShotSize | str:
    """Map a free-text framing onto :class:`ShotSize`, else return the cleaned text."""
    match = _SHOT_ALIASES.get(_norm(value), _UNMATCHED)
    return match if match is not _UNMATCHED else value.strip()  # type: ignore[return-value]


def coerce_speed(value: str) -> CameraSpeed | str:
    """Map a free-text speed onto :class:`CameraSpeed`, else return the cleaned text."""
    match = _SPEED_ALIASES.get(_norm(value), _UNMATCHED)
    return match if match is not _UNMATCHED else value.strip()  # type: ignore[return-value]


class CameraDirection(BaseModel):
    """The canonical camera block: framing + move + speed (+ optional angle).

    Mirrors the three fields of :class:`app.agents.contracts.Camera`
    (``shot_size``/``move``/``speed``) plus an optional ``angle`` the agents'
    block does not yet carry. Each is a canonical enum *or* free text — a value
    that does not map to an enum (e.g. a bespoke "whip pan into a snap zoom")
    survives verbatim so a dialect can still place it.
    """

    model_config = ConfigDict(extra="forbid")

    shot_size: ShotSize | str = ShotSize.MEDIUM
    move: CameraMove | str = CameraMove.STATIC
    speed: CameraSpeed | str = CameraSpeed.MEDIUM
    angle: CameraAngle | str | None = None

    @classmethod
    def from_agent_camera(
        cls,
        *,
        shot_size: str,
        move: str,
        speed: str,
        angle: str | None = None,
    ) -> CameraDirection:
        """Build from the agents' raw (string) :class:`Camera` fields, coercing each.

        This is the bridge from :class:`app.agents.contracts.Camera` without
        importing it (the prompts layer stays self-contained / dependency-light).
        """
        return cls(
            shot_size=coerce_shot_size(shot_size),
            move=coerce_move(move),
            speed=coerce_speed(speed),
            angle=angle.strip() if angle and angle.strip() else None,
        )


class RenderIntent(StrEnum):
    """The model-neutral generation mode (parallels the §9.3 Wan modes).

    A dialect that distinguishes text-to-video from image-to-video phrasing reads
    this; most dialects ignore it (the prompt text is the same), but it lets a
    dialect, e.g., drop a "first frame:"-style continuity preamble for pure t2v.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


class ShotDescription(BaseModel):
    """The canonical, model-agnostic description of one shot to render (pure data).

    This is the single source of truth a :class:`~app.video.prompts.base.PromptDialect`
    translates. It is deliberately richer than any one model's prompt: a dialect
    selects and phrases the subset its model understands, and the length-aware
    compressor drops the lowest-priority parts last when a budget bites.

    Field priority for compression, high → low: ``action``/``subject`` (never
    dropped), ``setting``, ``camera``, ``mood``, ``lighting``, ``style_refs``,
    ``quality_tokens``, ``continuity_tags``. ``negative_cues`` live in their own
    channel and never compete with the positive prompt for budget.
    """

    model_config = ConfigDict(extra="forbid")

    #: Who/what is on screen — the core entity the shot is about (rarely dropped).
    subject: str = ""
    #: What happens — the motion/event. The single most important line for video.
    action: str = ""
    #: Where it happens — location / environment.
    setting: str = ""
    #: The emotional register ("tense", "wistful", "triumphant").
    mood: str = ""
    #: How it is lit ("low-key chiaroscuro", "soft window light").
    lighting: str = ""
    #: The camera block (framing + move + speed + angle).
    camera: CameraDirection = Field(default_factory=CameraDirection)
    #: Style references — director/look/lens fragments ("noir", "anamorphic flare").
    style_refs: list[str] = Field(default_factory=list)
    #: Quality / fidelity tokens ("cinematic", "film grain", "volumetric light").
    quality_tokens: list[str] = Field(default_factory=list)
    #: Continuity anchors (entity@version refs, "matches previous shot") — kept for
    #: cross-shot consistency; phrased only by dialects that benefit from it.
    continuity_tags: list[str] = Field(default_factory=list)
    #: Things to avoid. Dialects that support a negative prompt emit these in the
    #: native channel; dialects without one fold a short avoid-clause into the text.
    negative_cues: list[str] = Field(default_factory=list)
    #: The generation mode (dialects that branch on it read it; most ignore it).
    intent: RenderIntent = RenderIntent.TEXT_TO_VIDEO
    #: Target clip length in seconds (advisory; some dialects emit it as a param).
    duration_s: float = 5.0
    #: Deterministic seed (emitted by dialects whose models accept one inline).
    seed: int | None = None


__all__ = [
    "CameraAngle",
    "CameraDirection",
    "CameraMove",
    "CameraSpeed",
    "RenderIntent",
    "ShotDescription",
    "ShotSize",
    "coerce_move",
    "coerce_shot_size",
    "coerce_speed",
]
