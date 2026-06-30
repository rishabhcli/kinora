"""Per-dialect camera/film-grammar vocabularies + small phrasing helpers.

Each video model has its own idiomatic way to name a camera move or framing. This
module holds the translation tables — one per dialect — that turn the canonical
:class:`~app.video.prompts.canonical.CameraMove` / :class:`ShotSize` /
:class:`CameraSpeed` / :class:`CameraAngle` enums into that model's preferred
words. The Wan table is exactly the phrasing in
:func:`app.agents.generator.compose_wan_prompt` (so the Wan dialect reproduces
today's output byte-for-byte); the others use each model's documented grammar.

Pure data + tiny pure helpers. No I/O.
"""

from __future__ import annotations

from .canonical import (
    CameraAngle,
    CameraDirection,
    CameraMove,
    CameraSpeed,
    ShotSize,
)

# --------------------------------------------------------------------------- #
# Wan (DashScope) — faithful to generator.compose_wan_prompt
#
# The generator collapses several aliases onto one phrase (e.g. push/push_in/
# dolly_in -> "slow dolly push-in"). Our canonical enum already disambiguates, so
# each enum member maps to the exact phrase the generator would have produced for
# that alias. PAN_LEFT/PAN_RIGHT both -> "smooth horizontal pan" (the generator's
# "pan" phrase), TILT_UP/TILT_DOWN both -> "deliberate vertical tilt", CRANE_UP/
# CRANE_DOWN both -> "sweeping crane move" (the generator had no directional split).
# --------------------------------------------------------------------------- #
WAN_MOVE: dict[CameraMove, str] = {
    CameraMove.STATIC: "locked-off static frame",
    CameraMove.PUSH_IN: "slow dolly push-in",
    CameraMove.PULL_OUT: "smooth dolly pull-back",
    CameraMove.PAN_LEFT: "smooth horizontal pan",
    CameraMove.PAN_RIGHT: "smooth horizontal pan",
    CameraMove.TILT_UP: "deliberate vertical tilt",
    CameraMove.TILT_DOWN: "deliberate vertical tilt",
    CameraMove.TRACK: "tracking shot gliding alongside the action",
    CameraMove.FOLLOW: "tracking shot following the subject",
    CameraMove.ORBIT: "orbiting arc around the subject",
    CameraMove.CRANE_UP: "sweeping crane move",
    CameraMove.CRANE_DOWN: "sweeping crane move",
    CameraMove.HANDHELD: "subtle handheld energy",
    CameraMove.ZOOM: "deliberate zoom",
    CameraMove.ZOOM_IN: "deliberate zoom-in",
    CameraMove.ZOOM_OUT: "deliberate zoom-out",
}
WAN_SHOT: dict[ShotSize, str] = {
    ShotSize.EXTREME_WIDE: "extreme wide vista",
    ShotSize.WIDE: "wide establishing shot",
    ShotSize.FULL: "full shot",
    ShotSize.MEDIUM: "medium shot",
    ShotSize.COWBOY: "medium cowboy framing",
    ShotSize.CLOSE_UP: "intimate close-up",
    ShotSize.EXTREME_CLOSE_UP: "extreme close-up",
}
WAN_SPEED: dict[CameraSpeed, str] = {
    CameraSpeed.SLOW: "slow and deliberate",
    CameraSpeed.MEDIUM: "steady",
    CameraSpeed.FAST: "energetic",
}

# --------------------------------------------------------------------------- #
# Runway Gen-3 — terse, technical camera grammar ("camera: dolly in")
# --------------------------------------------------------------------------- #
RUNWAY_MOVE: dict[CameraMove, str] = {
    CameraMove.STATIC: "locked camera",
    CameraMove.PUSH_IN: "dolly in",
    CameraMove.PULL_OUT: "dolly out",
    CameraMove.PAN_LEFT: "pan left",
    CameraMove.PAN_RIGHT: "pan right",
    CameraMove.TILT_UP: "tilt up",
    CameraMove.TILT_DOWN: "tilt down",
    CameraMove.TRACK: "tracking shot",
    CameraMove.FOLLOW: "follow cam",
    CameraMove.ORBIT: "orbit",
    CameraMove.CRANE_UP: "crane up",
    CameraMove.CRANE_DOWN: "crane down",
    CameraMove.HANDHELD: "handheld",
    CameraMove.ZOOM: "zoom",
    CameraMove.ZOOM_IN: "zoom in",
    CameraMove.ZOOM_OUT: "zoom out",
}
RUNWAY_SHOT: dict[ShotSize, str] = {
    ShotSize.EXTREME_WIDE: "extreme wide shot",
    ShotSize.WIDE: "wide shot",
    ShotSize.FULL: "full shot",
    ShotSize.MEDIUM: "medium shot",
    ShotSize.COWBOY: "cowboy shot",
    ShotSize.CLOSE_UP: "close-up",
    ShotSize.EXTREME_CLOSE_UP: "extreme close-up",
}
RUNWAY_SPEED: dict[CameraSpeed, str] = {
    CameraSpeed.SLOW: "slow",
    CameraSpeed.MEDIUM: "steady",
    CameraSpeed.FAST: "fast",
}

# --------------------------------------------------------------------------- #
# Kling — natural cinematic phrasing, dedicated negative prompt
# --------------------------------------------------------------------------- #
KLING_MOVE: dict[CameraMove, str] = {
    CameraMove.STATIC: "fixed camera",
    CameraMove.PUSH_IN: "camera pushes in",
    CameraMove.PULL_OUT: "camera pulls back",
    CameraMove.PAN_LEFT: "camera pans left",
    CameraMove.PAN_RIGHT: "camera pans right",
    CameraMove.TILT_UP: "camera tilts up",
    CameraMove.TILT_DOWN: "camera tilts down",
    CameraMove.TRACK: "tracking movement",
    CameraMove.FOLLOW: "camera follows the subject",
    CameraMove.ORBIT: "camera orbits the subject",
    CameraMove.CRANE_UP: "crane rises",
    CameraMove.CRANE_DOWN: "crane descends",
    CameraMove.HANDHELD: "handheld camera",
    CameraMove.ZOOM: "zoom",
    CameraMove.ZOOM_IN: "zoom in",
    CameraMove.ZOOM_OUT: "zoom out",
}
KLING_SHOT: dict[ShotSize, str] = dict(RUNWAY_SHOT)
KLING_SPEED: dict[CameraSpeed, str] = {
    CameraSpeed.SLOW: "slowly",
    CameraSpeed.MEDIUM: "smoothly",
    CameraSpeed.FAST: "quickly",
}

# --------------------------------------------------------------------------- #
# Luma Dream Machine — flowing natural language ("the camera glides in")
# --------------------------------------------------------------------------- #
LUMA_MOVE: dict[CameraMove, str] = {
    CameraMove.STATIC: "the camera holds still",
    CameraMove.PUSH_IN: "the camera glides in",
    CameraMove.PULL_OUT: "the camera drifts back",
    CameraMove.PAN_LEFT: "the camera pans to the left",
    CameraMove.PAN_RIGHT: "the camera pans to the right",
    CameraMove.TILT_UP: "the camera tilts upward",
    CameraMove.TILT_DOWN: "the camera tilts downward",
    CameraMove.TRACK: "the camera travels alongside",
    CameraMove.FOLLOW: "the camera follows behind",
    CameraMove.ORBIT: "the camera arcs around",
    CameraMove.CRANE_UP: "the camera cranes upward",
    CameraMove.CRANE_DOWN: "the camera cranes downward",
    CameraMove.HANDHELD: "the handheld camera breathes with the action",
    CameraMove.ZOOM: "the lens zooms",
    CameraMove.ZOOM_IN: "the lens zooms in",
    CameraMove.ZOOM_OUT: "the lens zooms out",
}
LUMA_SHOT: dict[ShotSize, str] = {
    ShotSize.EXTREME_WIDE: "an extreme wide vista",
    ShotSize.WIDE: "a wide establishing frame",
    ShotSize.FULL: "a full-body frame",
    ShotSize.MEDIUM: "a medium frame",
    ShotSize.COWBOY: "a cowboy frame",
    ShotSize.CLOSE_UP: "an intimate close-up",
    ShotSize.EXTREME_CLOSE_UP: "an extreme close-up",
}
LUMA_SPEED: dict[CameraSpeed, str] = {
    CameraSpeed.SLOW: "slowly",
    CameraSpeed.MEDIUM: "smoothly",
    CameraSpeed.FAST: "swiftly",
}

# --------------------------------------------------------------------------- #
# Veo / Sora — rich descriptive cinematography prose
# --------------------------------------------------------------------------- #
PROSE_MOVE: dict[CameraMove, str] = {
    CameraMove.STATIC: "a locked-off static camera",
    CameraMove.PUSH_IN: "a slow dolly push-in",
    CameraMove.PULL_OUT: "a smooth dolly pull-back",
    CameraMove.PAN_LEFT: "a measured pan to the left",
    CameraMove.PAN_RIGHT: "a measured pan to the right",
    CameraMove.TILT_UP: "a deliberate upward tilt",
    CameraMove.TILT_DOWN: "a deliberate downward tilt",
    CameraMove.TRACK: "a tracking shot gliding alongside the action",
    CameraMove.FOLLOW: "a following shot trailing the subject",
    CameraMove.ORBIT: "an orbiting arc around the subject",
    CameraMove.CRANE_UP: "a sweeping crane move rising overhead",
    CameraMove.CRANE_DOWN: "a sweeping crane move descending",
    CameraMove.HANDHELD: "subtle handheld energy",
    CameraMove.ZOOM: "a deliberate zoom",
    CameraMove.ZOOM_IN: "a deliberate zoom-in",
    CameraMove.ZOOM_OUT: "a deliberate zoom-out",
}
PROSE_SHOT: dict[ShotSize, str] = {
    ShotSize.EXTREME_WIDE: "an extreme wide vista",
    ShotSize.WIDE: "a wide establishing shot",
    ShotSize.FULL: "a full shot",
    ShotSize.MEDIUM: "a medium shot",
    ShotSize.COWBOY: "a medium cowboy framing",
    ShotSize.CLOSE_UP: "an intimate close-up",
    ShotSize.EXTREME_CLOSE_UP: "an extreme close-up",
}
PROSE_SPEED: dict[CameraSpeed, str] = {
    CameraSpeed.SLOW: "slow and deliberate",
    CameraSpeed.MEDIUM: "steady",
    CameraSpeed.FAST: "energetic",
}

# --------------------------------------------------------------------------- #
# Camera angle phrasing (shared; neutral EYE_LEVEL is omitted by callers)
# --------------------------------------------------------------------------- #
ANGLE_PHRASE: dict[CameraAngle, str] = {
    CameraAngle.EYE_LEVEL: "eye-level angle",
    CameraAngle.LOW_ANGLE: "low angle",
    CameraAngle.HIGH_ANGLE: "high angle",
    CameraAngle.BIRDS_EYE: "bird's-eye view",
    CameraAngle.DUTCH: "dutch tilt",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def lookup_move(camera: CameraDirection, table: dict[CameraMove, str]) -> str:
    """The model's word for the camera's move; free-text moves pass through cleaned."""
    move = camera.move
    if isinstance(move, CameraMove):
        return table[move]
    return str(move).strip()


def lookup_shot(camera: CameraDirection, table: dict[ShotSize, str]) -> str:
    """The model's word for the camera's framing; free-text framings pass through."""
    shot = camera.shot_size
    if isinstance(shot, ShotSize):
        return table[shot]
    return str(shot).strip()


def lookup_speed(camera: CameraDirection, table: dict[CameraSpeed, str]) -> str:
    """The model's word for the camera's speed; free-text speeds pass through."""
    speed = camera.speed
    if isinstance(speed, CameraSpeed):
        return table[speed]
    return str(speed).strip()


def lookup_angle(camera: CameraDirection) -> str:
    """The phrase for a non-neutral camera angle, else ``""`` (EYE_LEVEL / None)."""
    angle = camera.angle
    if angle is None:
        return ""
    if isinstance(angle, CameraAngle):
        return "" if angle is CameraAngle.EYE_LEVEL else ANGLE_PHRASE[angle]
    return str(angle).strip()


__all__ = [
    "ANGLE_PHRASE",
    "KLING_MOVE",
    "KLING_SHOT",
    "KLING_SPEED",
    "LUMA_MOVE",
    "LUMA_SHOT",
    "LUMA_SPEED",
    "PROSE_MOVE",
    "PROSE_SHOT",
    "PROSE_SPEED",
    "RUNWAY_MOVE",
    "RUNWAY_SHOT",
    "RUNWAY_SPEED",
    "WAN_MOVE",
    "WAN_SHOT",
    "WAN_SPEED",
    "lookup_angle",
    "lookup_move",
    "lookup_shot",
    "lookup_speed",
]
